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
import sys
import time
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx

from clinic_desk.clinic import load_clinic
from clinic_desk.coach.loop import Pass, Plan, Split, coach_loop
from clinic_desk.coach.propose import Edit, name_words, propose
from clinic_desk.config import ModelChoice, load_config
from clinic_desk.env import load_env, require
from clinic_desk.llm.base import AssistantMessage, Message, ToolResults
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
from clinic_desk.simulator.evaluate import judge_pass, load_verdicts
from clinic_desk.simulator.gate import compare
from clinic_desk.simulator.gate import to_markdown as gate_markdown
from clinic_desk.simulator.judge import Verdict
from clinic_desk.simulator.persona import load_personas, load_split
from clinic_desk.simulator.report import ReportData, analyse, to_html, to_markdown
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
    split: str,
    only: list[str],
    repeats: int,
    concurrency: int,
    label: str,
    yes: bool,
    judge_role: str | None = "judge",
    prompt_file: str | None = None,
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
    if judge_role is not None:
        if judge_role not in roles:
            raise SystemExit(f"config: no llm.{judge_role} role (or use --no-judge)")
        j = roles[judge_role]
        prices["judge"] = (j.usd_per_mtok_in, j.usd_per_mtok_out)
    planned = budget.estimate(len(personas) * repeats, prices)
    judged_by = f" + judge {roles[judge_role].model}" if judge_role else " (no judge)"
    print(f"{len(personas)} personas x {repeats} repeats{judged_by} · estimate ~${planned:.4f}")
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
            prompt=Path(prompt_file).read_text("utf-8") if prompt_file else None,
        )
    finally:
        for m in models.values():
            await m.client.aclose()
        if exporter is not None:
            await exporter.aclose()
    verdicts = None
    if judge_role is not None:
        verdicts, _, usd = await _judge_conversations(
            run_id, conversations, judge_role, roles[judge_role], concurrency
        )
        print(f"judge: ${usd:.4f}")
    text = summary(run_id, conversations, langfuse_trace_url(), verdicts)
    (RUNS / run_id / "summary.md").write_text(text + "\n", "utf-8")
    print(text)
    print(f"\nsaved to runs/sim/{run_id}/ · {budget.check(Decimal(0))}")


async def _judge_conversations(
    run_id: str,
    conversations: list[Conversation],
    role: str,
    choice: ModelChoice,
    concurrency: int,
) -> tuple[dict[str, Verdict], dict[str, Verdict], Decimal]:
    """Judge + lint on a pass; verdicts saved next to the conversations; cost to the ledger."""
    clinic = load_clinic()
    personas = {p.id: p for p in load_personas(clinic)}
    calls: list[Call] = []
    llm = Metered(
        build_llm(choice), "judge", choice.usd_per_mtok_in, choice.usd_per_mtok_out, calls
    )
    traced = any(c.trace_id for c in conversations)
    exporter = LangfuseExporter.from_env() if traced else None
    try:
        merged, alone = await judge_pass(
            conversations,
            llm,
            run_id=run_id,
            folder=RUNS / run_id,
            role=role,
            personas=personas,
            clinic=clinic,
            mode=choice.structured_output,
            concurrency=concurrency,
            exporter=exporter,
        )
    finally:
        await llm.inner.aclose()
        if exporter is not None:
            await exporter.aclose()
        budget.record(calls, f"{run_id}/judge-{role}")
    return merged, alone, sum((c.usd for c in calls), Decimal(0))


async def judge_run(
    run_id: str, role: str, labelled_only: bool, concurrency: int, yes: bool
) -> None:
    """Judge the conversations of a saved pass with the model of config role `role`."""
    load_env()
    roles = load_config().llm
    if role not in roles:
        raise SystemExit(f"config: no llm.{role} role (see config/example.yaml)")
    all_conversations = load_run(run_id)
    conversations = [c for c in all_conversations if c.counted]
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
    merged, alone, usd = await _judge_conversations(
        run_id, conversations, role, choice, concurrency
    )
    ok = [v for v in merged.values() if not v.error]
    print(f"judge pass {sum(v.passed for v in ok)}/{len(ok)} · errors {len(merged) - len(ok)}")
    print(f"cost ${usd:.4f} · {budget.check(Decimal(0))}")
    if not labelled_only:  # the whole pass judged: the summary gets the judge columns
        text = summary(run_id, all_conversations, langfuse_trace_url(), merged)
        (RUNS / run_id / "summary.md").write_text(text + "\n", "utf-8")
    if labelled:
        print()
        labels = load_labels()
        table = agreement(labels, alone)
        print("judge alone (no lint):")
        print(table.split("\n\n")[0])
        print("\njudge + lint:")
        print(agreement(labels, merged))


def report_cmd(run_id: str, role: str) -> None:
    """Write report.md and report.html for a judged pass (no model calls)."""
    load_env()  # only for the Langfuse project id in the trace links
    folder = RUNS / run_id
    split_of = {pid: name for name, ids in load_split().items() for pid in ids}
    personas = {p.id: p for p in load_personas(load_clinic())}
    data = analyse(
        run_id,
        load_run(run_id),
        load_verdicts(run_id, role, folder),
        split_of,
        personas,
        langfuse_trace_url(),
    )
    (folder / "report.md").write_text(to_markdown(data), "utf-8")
    (folder / "report.html").write_text(to_html(data), "utf-8")
    print(to_markdown(data))
    print(f"saved {folder / 'report.md'} and {folder / 'report.html'}")


def gate_cmd(baseline: str, candidate: str, role: str) -> None:
    """Compare two judged passes; write gate.md into the candidate's folder; exit 1 on fail."""
    load_env()
    split_of = {pid: name for name, ids in load_split().items() for pid in ids}
    personas = {p.id: p for p in load_personas(load_clinic())}
    passes = []
    for run_id in (baseline, candidate):
        conversations = load_run(run_id)
        verdicts = load_verdicts(run_id, role, RUNS / run_id)
        if not verdicts:
            raise SystemExit(f"{run_id}: no verdicts of llm.{role} (clinic-desk judge {run_id})")
        data = analyse(run_id, conversations, verdicts, split_of, personas, langfuse_trace_url())
        passes.append((data, conversations))
    try:
        result = compare(passes[0], passes[1])
    except ValueError as exc:
        raise SystemExit(str(exc)) from exc
    text = gate_markdown(result)
    (RUNS / candidate / "gate.md").write_text(text, "utf-8")
    print(text)
    print(f"saved {RUNS / candidate / 'gate.md'}")
    if not result.passed:
        raise SystemExit(1)


COACH_CALL_USD = Decimal("0.15")  # one proposal, worst case: 2 attempts x ~25k in / ~5k out


async def coach_cmd(
    iterations: int,
    plan: Plan,
    baseline: str | None,
    prompt_file: str,
    concurrency: int,
    yes: bool,
) -> None:
    """The coach loop on real models: prints the estimate, needs --yes, writes runs/coach/<id>/."""
    load_env()
    roles = load_config().llm
    for need in ("receptionist", "persona", "judge", "coach"):
        if need not in roles:
            raise SystemExit(f"config: add an llm.{need} role (see config/example.yaml)")
    clinic = load_clinic()
    all_personas = load_personas(clinic)
    split = load_split()
    split_of = {pid: name for name, ids in split.items() for pid in ids}
    by_id = {p.id: p for p in all_personas}
    start = Path(prompt_file).read_text("utf-8")

    vis0 = None
    have = 0  # repeats the reused baseline already has; it is topped up to plan.base_repeats
    if baseline:
        saved = RUNS / baseline / "prompt.md"
        if not saved.exists() or saved.read_text("utf-8") != start:
            raise SystemExit(f"{baseline} did not measure {prompt_file}: drop --baseline")
        have = 1 + max(c.repeat for c in load_run(baseline))

    used = ["receptionist", "persona", "judge"] + (["translator"] if "translator" in roles else [])
    prices = {r: (roles[r].usd_per_mtok_in, roles[r].usd_per_mtok_out) for r in used}
    if "translator" not in roles:
        prices["translator"] = prices["receptionist"]
    margin = Decimal("1.2")  # the estimate ran ~12% under the real cost on 2026-11-15

    def cost(which: Split, repeats: int) -> Decimal:
        return budget.estimate(len(split[which]) * repeats, prices) * margin

    top_up = max(plan.base_repeats - have, 0)
    base = cost("visible", top_up) if top_up else Decimal(0)
    cand = cost("visible", plan.repeats)
    more = cost("visible", plan.more)
    ho = cost("held_out", plan.held_out_repeats)
    planned = base + ho + iterations * (cand + more + ho + COACH_CALL_USD)
    likely = base + iterations * (cand + more / 2 + COACH_CALL_USD) + ho * 2
    print(
        f"coach: {iterations} iterations · start ~${base:.2f} ({plan.base_repeats} repeats"
        f"{f', {have} reused' if have else ''}) ·"
        f" candidate ~${cand:.2f} ({plan.repeats}) + ~${more:.2f} if promising ({plan.more}) ·"
        f" held-out ~${ho:.2f} ({plan.held_out_repeats}) per prompt, only for a winner ·"
        f" proposal ~${COACH_CALL_USD}\n"
        f"  likely (half extended, one winner) ~${likely:.2f} ·"
        f" worst case (every candidate extended and winning) ~${planned:.2f}"
    )
    print(budget.check(planned))
    if not yes:
        raise SystemExit("not started: this run costs money; re-run with --yes")

    def role(name: str) -> RoleModel:
        c = roles[name]
        return RoleModel(build_llm(c), c.usd_per_mtok_in, c.usd_per_mtok_out)

    models = {
        name: role(name)
        for name in ("receptionist", "persona", *(["translator"] if "translator" in roles else []))
    }
    exporter = LangfuseExporter.from_env()
    coach_id = new_run_id("coach")
    out = Path("runs/coach") / coach_id
    calls: list[Call] = []
    coach_llm = Metered(
        build_llm(roles["coach"]),
        "coach",
        roles["coach"].usd_per_mtok_in,
        roles["coach"].usd_per_mtok_out,
        calls,
    )
    names = name_words([p.caller.name for p in all_personas])

    categories: dict[str, str] = {p.id: p.category for p in all_personas}

    async def evaluate(prompt: str, which: Split, label: str, repeats: int) -> Pass:
        print(budget.check(cost(which, repeats)))  # raises past the cap
        personas = [by_id[pid] for pid in split[which]]
        run_id, conversations = await run_pass(
            personas,
            receptionist=models["receptionist"],
            persona_model=models["persona"],
            translator=models.get("translator"),
            repeats=repeats,
            concurrency=concurrency,
            run_id=new_run_id(f"{coach_id.split('-', 2)[-1]}-{label}"),
            exporter=exporter,
            split_of=split_of,
            prompt=prompt,
        )
        if not any(c.counted for c in conversations):  # 2026-11-15: Mistral 503 on all 28
            errors = sorted({(c.error or c.end)[:120] for c in conversations})
            raise RuntimeError(f"{run_id}: no conversation counted ({'; '.join(errors)})")
        verdicts, _, _ = await _judge_conversations(
            run_id, conversations, "judge", roles["judge"], concurrency
        )
        data = analyse(run_id, conversations, verdicts, split_of, by_id, langfuse_trace_url())
        score = next(s for s in data.splits if s.split != "all").criteria
        print(f"  {label}: {run_id} · criteria {score}")
        return Pass(run_id, data, conversations)

    async def extend(p: Pass, more: int) -> Pass:
        """More repeats of the same prompt into the same pass, judged, then analysed as one."""
        print(budget.check(cost("visible", more)))
        personas = sorted({by_id[c.persona_id].id for c in p.conversations})
        _, added = await run_pass(
            [by_id[pid] for pid in personas],
            receptionist=models["receptionist"],
            persona_model=models["persona"],
            translator=models.get("translator"),
            repeats=more,
            concurrency=concurrency,
            run_id=p.run_id,
            exporter=exporter,
            split_of=split_of,
            prompt=(RUNS / p.run_id / "prompt.md").read_text("utf-8"),
            first_repeat=max(c.repeat for c in p.conversations) + 1,
        )
        await _judge_conversations(p.run_id, added, "judge", roles["judge"], concurrency)
        everything = [*p.conversations, *added]
        verdicts = load_verdicts(p.run_id, "judge", RUNS / p.run_id)
        (RUNS / p.run_id / "summary.md").write_text(
            summary(p.run_id, everything, langfuse_trace_url(), verdicts) + "\n", "utf-8"
        )
        data = analyse(p.run_id, everything, verdicts, split_of, by_id, langfuse_trace_url())
        score = next(s for s in data.splits if s.split != "all").criteria
        print(f"  extended {p.run_id} by {more} repeats · criteria {score}")
        return Pass(p.run_id, data, everything)

    async def propose_one(prompt: str, data: ReportData, history: str) -> tuple[Edit, str]:
        try:
            edit, candidate = await propose(coach_llm, prompt, data, history, names, categories)
        finally:
            budget.record(calls, f"{coach_id}/coach")
            calls.clear()
        print(f"  proposal: target {edit.target} · {edit.reason[:160]}")
        return edit, candidate

    if baseline:
        verdicts = load_verdicts(baseline, "judge", RUNS / baseline)
        conversations = load_run(baseline)
        data = analyse(baseline, conversations, verdicts, split_of, by_id, langfuse_trace_url())
        vis0 = Pass(baseline, data, conversations)
        if top_up:
            vis0 = await extend(vis0, top_up)
    try:
        _, entries = await coach_loop(
            start,
            propose=propose_one,
            evaluate=evaluate,
            extend=extend,
            iterations=iterations,
            out=out,
            plan=plan,
            visible0=vis0,
        )
    finally:
        for m in models.values():
            await m.client.aclose()
        await coach_llm.inner.aclose()
        if exporter is not None:
            await exporter.aclose()
    print((out / "changelog.md").read_text("utf-8"))
    print(f"saved to {out} · {sum(e.kept for e in entries)} kept · {budget.check(Decimal(0))}")
    print(f"best prompt: {out / 'best.md'} (prompts/receptionist.md is unchanged)")


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
    # Windows consoles default to cp1252: the summary's ✅/❌ and Catalan text must not crash
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
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
    sim.add_argument("--judge", default="judge", help="config llm role that judges the pass")
    sim.add_argument("--no-judge", action="store_true", help="code checks only, no judge")
    sim.add_argument("--prompt", help="receptionist prompt file (default prompts/receptionist.md)")
    co = sub.add_parser("coach", help="coach loop: one prompt change at a time (paid)")
    co.add_argument("--iterations", type=int, default=5)
    co.add_argument("--repeats", type=int, default=2, help="per candidate on visible")
    co.add_argument("--base-repeats", type=int, default=4, help="the start prompt on visible")
    co.add_argument("--more", type=int, default=2, help="added to a promising candidate")
    co.add_argument("--held-out-repeats", type=int, default=4)
    co.add_argument("--baseline", help="a judged visible pass of the start prompt, to reuse")
    co.add_argument("--prompt", default="prompts/receptionist.md", help="start prompt")
    co.add_argument("--concurrency", type=int, default=4)
    co.add_argument("--yes", action="store_true", help="run even though it costs money")
    gat = sub.add_parser("gate", help="regression gate: candidate pass vs baseline (free)")
    gat.add_argument("baseline")
    gat.add_argument("candidate")
    gat.add_argument("--role", default="judge", help="whose verdicts to compare")
    jdg = sub.add_parser("judge", help="judge a saved pass (real model from config; paid)")
    jdg.add_argument("run_id")
    jdg.add_argument("--role", default="judge", help="config llm role of the judge model")
    jdg.add_argument("--labelled", action="store_true", help="only hand-labelled conversations")
    jdg.add_argument("--concurrency", type=int, default=4)
    jdg.add_argument("--yes", action="store_true", help="run even though it costs money")
    rep = sub.add_parser("report", help="eval report of a judged pass (free: no model calls)")
    rep.add_argument("run_id")
    rep.add_argument("--role", default="judge", help="whose verdicts to report")
    lab = sub.add_parser("label-template", help="pick conversations to hand-label for the judge")
    lab.add_argument("run_id")
    lab.add_argument("--n", type=int, default=20)
    lab.add_argument("--force", action="store_true")
    args = parser.parse_args()
    if args.command == "judge":
        asyncio.run(judge_run(args.run_id, args.role, args.labelled, args.concurrency, args.yes))
    if args.command == "report":
        report_cmd(args.run_id, args.role)
    if args.command == "coach":
        asyncio.run(
            coach_cmd(
                args.iterations,
                Plan(args.base_repeats, args.repeats, args.more, args.held_out_repeats),
                args.baseline,
                args.prompt,
                args.concurrency,
                args.yes,
            )
        )
    if args.command == "gate":
        gate_cmd(args.baseline, args.candidate, args.role)
    if args.command == "label-template":
        label_template_cmd(args.run_id, args.n, args.force)
    if args.command == "simulate":
        asyncio.run(
            simulate(
                args.split,
                args.persona,
                args.repeats,
                args.concurrency,
                args.label,
                args.yes,
                judge_role=None if args.no_judge else args.judge,
                prompt_file=args.prompt,
            )
        )
    if args.command == "eval-retrieval":
        asyncio.run(eval_retrieval(args.embeddings))
    if args.command == "chat":
        now = datetime.fromisoformat(args.now).replace(tzinfo=ZoneInfo("Europe/Madrid"))
        asyncio.run(chat(now, args.lang, args.trace))


if __name__ == "__main__":
    main()
