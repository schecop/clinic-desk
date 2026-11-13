"""`clinic-desk chat`: talk to the receptionist on the seeded fake calendar.

Uses the receptionist model from config (`llm.receptionist`), so it calls a real (paid) API.
The clock is fixed (default Wed 11 Nov 2026, 10:00) so the seeded appointments make sense.
`--trace` prints each turn's tool calls and results, until conversations become Langfuse
traces with the simulator (D3).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import time
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx

from clinic_desk.clinic import load_clinic
from clinic_desk.config import load_config
from clinic_desk.env import load_env, require
from clinic_desk.llm.base import AssistantMessage, LLMError, Message, ToolResults
from clinic_desk.llm.factory import build_llm
from clinic_desk.llm.metered import Call, Metered
from clinic_desk.receptionist.graph import Receptionist, make_desk
from clinic_desk.receptionist.state import State
from clinic_desk.retrieval import BM25, Overlap, Retriever, chunks_from
from clinic_desk.retrieval.dense import Dense, cached, openai_embedder
from clinic_desk.retrieval.evaluate import all_in_prompt_tokens, evaluate, load_questions, report
from clinic_desk.simulator import budget
from clinic_desk.simulator.calibration import (
    LABELS,
    agreement,
    conversation_lang,
    label_template,
    load_labels,
    pick,
    stem,
)
from clinic_desk.simulator.judge import Verdict, judge
from clinic_desk.simulator.persona import load_personas, load_split
from clinic_desk.simulator.runner import Conversation, RoleModel
from clinic_desk.simulator.simulate import (
    RUNS,
    langfuse_trace_url,
    load_run,
    new_run_id,
    run_pass,
    summary,
)
from clinic_desk.tracing import LangfuseExporter

DEFAULT_NOW = "2026-11-11T10:00"


async def chat(now: datetime, lang: str, trace: bool) -> None:
    load_env()
    config = load_config()
    roles, settings = config.llm, config.receptionist
    choice = roles["receptionist"]
    llm = build_llm(choice)
    # the language guard's model; without its own role it reuses the receptionist's
    translator = build_llm(roles["translator"]) if "translator" in roles else None
    embed_client, retriever = None, None
    if settings.policies == "search" and settings.retriever != "bm25":
        embed_client, retriever = await _dense_retriever(settings.retriever)
    desk = make_desk(
        llm, lambda: now, translator=translator, policies=settings.policies, retriever=retriever
    )
    receptionist = Receptionist(desk, "ca" if lang == "ca" else "es")
    policies = settings.policies + (
        f"/{settings.retriever}" if settings.policies == "search" else ""
    )
    print(
        f"[{choice.provider}/{choice.model} · policies {policies} · fake calendar"
        f" · now {now:%a %Y-%m-%d %H:%M}]"
    )
    print("[empty line or Ctrl-C to quit]\n")
    try:
        while True:
            text = input("patient> ").strip()
            if not text:
                break
            seen = len(receptionist.state["messages"])
            started = time.perf_counter()
            reply = await receptionist.reply(text)
            took = time.perf_counter() - started
            if trace:
                _print_trace(receptionist.state["messages"][seen:], receptionist.state, took)
            print(f"desk> {reply}\n")
    except (KeyboardInterrupt, EOFError):
        pass
    finally:
        await llm.aclose()
        if translator is not None:
            await translator.aclose()
        if embed_client is not None:
            await embed_client.aclose()
    print(f"\nupcoming in the fake calendar: {await _summary(receptionist)}")


async def _dense_retriever(model: str) -> tuple[httpx.AsyncClient, Dense]:
    """An OpenAI embedding retriever over the policies; the caller closes the client."""
    headers = {"Authorization": f"Bearer {require('OPENAI_API_KEY')}"}
    client = httpx.AsyncClient(base_url="https://api.openai.com/v1", headers=headers, timeout=60)
    embed = cached(openai_embedder(client, model), Path(f"runs/embeddings/{model}.json"))
    return client, await Dense.build(model, chunks_from(load_clinic()), embed)


def _print_trace(new: list[Message], state: State, seconds: float) -> None:
    for message in new:
        if isinstance(message, AssistantMessage):
            for c in message.tool_calls:
                print(f"  · call {c.name}({json.dumps(c.arguments, ensure_ascii=False)})")
        elif isinstance(message, ToolResults):
            for r in message.results:
                body = r.content if len(r.content) <= 300 else r.content[:300] + "…"
                print(f"  {'✗' if r.is_error else '→'} {r.name}: {body}")
    pending = state["pending"].kind if state["pending"] else None
    fix = f" · lang fix {state['lang_fix']}" if state["lang_fix"] else ""
    print(
        f"  [lang {state['lang']}{fix} · confirm {state['confirmation']} · pending {pending}"
        f" · {state['steps']} model calls · {seconds:.1f}s]"
    )


async def _summary(receptionist: Receptionist) -> list[str]:
    patient_id = receptionist.state["patient_id"]
    if patient_id is None:
        return []
    return [
        f"{a.id} {a.service_id} {a.staff_id} {a.start:%a %d %H:%M}"
        for a in await receptionist.desk.scheduler.upcoming(patient_id)
    ]


async def eval_retrieval(embeddings: list[str]) -> None:
    """Lexical retrievers always (free); each `--embeddings MODEL` adds a paid OpenAI call."""
    clinic = load_clinic()
    chunks = chunks_from(clinic)
    questions = load_questions(clinic)
    retrievers: list[Retriever] = [Overlap(chunks), BM25(chunks, stem=None), BM25(chunks)]
    usage: list[int] = []
    if embeddings:
        load_env()
        headers = {"Authorization": f"Bearer {require('OPENAI_API_KEY')}"}
        async with httpx.AsyncClient(
            base_url="https://api.openai.com/v1", headers=headers, timeout=60
        ) as client:
            for model in embeddings:
                embed = cached(
                    openai_embedder(client, model, usage), Path(f"runs/embeddings/{model}.json")
                )
                dense = await Dense.build(model, chunks, embed)
                await dense.prepare([q.q for q in questions])
                retrievers.append(dense)
    print(report([evaluate(r, questions) for r in retrievers], all_in_prompt_tokens(clinic)))
    if embeddings:
        print()
        print(f"embedding tokens billed this run: {sum(usage)} (0 = all cached)")


async def simulate(
    split: str, only: list[str], repeats: int, concurrency: int, label: str, yes: bool
) -> None:
    """Print the estimate and the budget line; run only with --yes when it costs money."""
    load_env()
    roles = load_config().llm
    if "persona" not in roles:
        raise SystemExit("config: add an llm.persona role (see config/example.yaml)")
    personas = load_personas(load_clinic())
    if only:
        personas = [p for p in personas if p.id in only]
    elif split != "all":
        keep = set(load_split()[split])
        personas = [p for p in personas if p.id in keep]
    if not personas:
        raise SystemExit("no personas selected")
    used = ["receptionist", "persona"] + (["translator"] if "translator" in roles else [])
    prices = {r: (roles[r].usd_per_mtok_in, roles[r].usd_per_mtok_out) for r in used}
    if "translator" not in roles:  # the receptionist's model translates
        prices["translator"] = prices["receptionist"]
    planned = budget.estimate(len(personas) * repeats, prices)
    print(f"{len(personas)} personas x {repeats} repeats · estimate ~${planned:.4f}")
    print(budget.check(planned))
    if planned > 0 and not yes:
        raise SystemExit("not started: this run costs money; re-run with --yes")

    def role(name: str) -> RoleModel:
        c = roles[name]
        return RoleModel(build_llm(c), c.usd_per_mtok_in, c.usd_per_mtok_out)

    models = {name: role(name) for name in used}
    exporter = LangfuseExporter.from_env()
    print("traces: Langfuse" if exporter else "traces: off (no LANGFUSE_* keys in .env)")
    split_of = {pid: name for name, ids in load_split().items() for pid in ids}
    try:
        run_id, conversations = await run_pass(
            personas,
            receptionist=models["receptionist"],
            persona_model=models["persona"],
            translator=models.get("translator"),
            repeats=repeats,
            concurrency=concurrency,
            run_id=new_run_id(label),
            exporter=exporter,
            split_of=split_of,
        )
    finally:
        for m in models.values():
            await m.client.aclose()
        if exporter is not None:
            await exporter.aclose()
    print(summary(run_id, conversations, langfuse_trace_url()))
    print(f"\nsaved to runs/sim/{run_id}/ · {budget.check(Decimal(0))}")


async def judge_run(
    run_id: str, role: str, labelled_only: bool, concurrency: int, yes: bool
) -> None:
    """Judge the conversations of a saved pass with the model of config role `role`."""
    load_env()
    roles = load_config().llm
    if role not in roles:
        raise SystemExit(f"config: no llm.{role} role (see config/example.yaml)")
    clinic = load_clinic()
    personas = {p.id: p for p in load_personas(clinic)}
    conversations = [c for c in load_run(run_id) if c.counted]
    labelled = {x.conversation for x in load_labels()} if LABELS.exists() else set()
    if labelled_only:
        conversations = [c for c in conversations if f"{run_id}/{stem(c)}" in labelled]
    if not conversations:
        raise SystemExit("no conversations to judge")
    choice = roles[role]
    planned = budget.estimate(
        len(conversations), {"judge": (choice.usd_per_mtok_in, choice.usd_per_mtok_out)}
    )
    print(f"{len(conversations)} conversations · {choice.model} · estimate ~${planned:.4f}")
    print(budget.check(planned))
    if planned > 0 and not yes:
        raise SystemExit("not started: this run costs money; re-run with --yes")
    calls: list[Call] = []
    llm = Metered(
        build_llm(choice), "judge", choice.usd_per_mtok_in, choice.usd_per_mtok_out, calls
    )
    gate = asyncio.Semaphore(concurrency)
    folder = RUNS / run_id

    async def one(c: Conversation) -> Verdict:
        async with gate:
            name = f"{run_id}/{stem(c)}"
            try:
                v = await judge(llm, c, personas[c.persona_id], clinic, name)
            except LLMError as err:
                v = Verdict(conversation=name, model=choice.model, criteria=[], error=str(err))
        (folder / f"{stem(c)}.judge-{role}.json").write_text(v.model_dump_json(indent=1), "utf-8")
        return v

    try:
        verdicts = await asyncio.gather(*(one(c) for c in conversations))
    finally:
        await llm.inner.aclose()
        budget.record(calls, f"{run_id}/judge-{role}")
    usd = sum((c.usd for c in calls), Decimal(0))
    ok = [v for v in verdicts if not v.error]
    print(f"judge pass {sum(v.passed for v in ok)}/{len(ok)} · errors {len(verdicts) - len(ok)}")
    print(f"cost ${usd:.4f} · {budget.check(Decimal(0))}")
    if labelled:
        print()
        print(agreement(load_labels(), {v.conversation: v for v in verdicts}))


def label_template_cmd(run_id: str, n: int, force: bool) -> None:
    if LABELS.exists() and not force:
        raise SystemExit(f"{LABELS} exists (hand labels?); use --force to overwrite")
    personas = {p.id: p for p in load_personas(load_clinic())}
    chosen = pick(load_run(run_id), personas, n)
    LABELS.parent.mkdir(parents=True, exist_ok=True)
    LABELS.write_text(label_template(run_id, chosen, personas) + "\n", "utf-8")
    langs = [conversation_lang(personas[c.persona_id]) for c in chosen]
    print(
        f"{LABELS}: {len(chosen)} conversations ({langs.count('ca')} Catalan,"
        f" {sum(not c.code_pass for c in chosen)} code failures)"
    )


def main() -> None:
    parser = argparse.ArgumentParser(prog="clinic-desk")
    sub = parser.add_subparsers(dest="command", required=True)
    chat_cmd = sub.add_parser("chat", help="chat with the receptionist (real model, fake calendar)")
    chat_cmd.add_argument("--now", default=DEFAULT_NOW, help="local time, YYYY-MM-DDTHH:MM")
    chat_cmd.add_argument("--lang", default="es", choices=["es", "ca"])
    chat_cmd.add_argument("--trace", action="store_true", help="print tool calls and results")
    rag = sub.add_parser("eval-retrieval", help="hit@1/hit@3 of policy retrievers")
    rag.add_argument(
        "--embeddings",
        action="append",
        default=[],
        metavar="MODEL",
        help="also an OpenAI embedding model (paid, cached in runs/embeddings/)",
    )
    sim = sub.add_parser("simulate", help="a simulator pass (real models from config; paid)")
    sim.add_argument("--split", choices=["visible", "held_out", "all"], default="visible")
    sim.add_argument("--persona", action="append", default=[], help="only these persona ids")
    sim.add_argument("--repeats", type=int, default=1)
    sim.add_argument("--concurrency", type=int, default=4)
    sim.add_argument("--label", default="", help="suffix for the run id")
    sim.add_argument("--yes", action="store_true", help="run even though it costs money")
    jdg = sub.add_parser("judge", help="judge a saved pass (real model from config; paid)")
    jdg.add_argument("run_id")
    jdg.add_argument("--role", default="judge", help="config llm role of the judge model")
    jdg.add_argument("--labelled", action="store_true", help="only hand-labelled conversations")
    jdg.add_argument("--concurrency", type=int, default=4)
    jdg.add_argument("--yes", action="store_true", help="run even though it costs money")
    lab = sub.add_parser("label-template", help="pick conversations to hand-label for the judge")
    lab.add_argument("run_id")
    lab.add_argument("--n", type=int, default=20)
    lab.add_argument("--force", action="store_true")
    args = parser.parse_args()
    if args.command == "judge":
        asyncio.run(judge_run(args.run_id, args.role, args.labelled, args.concurrency, args.yes))
    if args.command == "label-template":
        label_template_cmd(args.run_id, args.n, args.force)
    if args.command == "simulate":
        asyncio.run(
            simulate(args.split, args.persona, args.repeats, args.concurrency, args.label, args.yes)
        )
    if args.command == "eval-retrieval":
        asyncio.run(eval_retrieval(args.embeddings))
    if args.command == "chat":
        now = datetime.fromisoformat(args.now).replace(tzinfo=ZoneInfo("Europe/Madrid"))
        asyncio.run(chat(now, args.lang, args.trace))


if __name__ == "__main__":
    main()
