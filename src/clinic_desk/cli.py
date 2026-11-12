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
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx

from clinic_desk.clinic import load_clinic
from clinic_desk.config import load_config
from clinic_desk.env import load_env, require
from clinic_desk.llm.base import AssistantMessage, Message, ToolResults
from clinic_desk.llm.factory import build_llm
from clinic_desk.receptionist.graph import Receptionist, make_desk
from clinic_desk.receptionist.state import State
from clinic_desk.retrieval import BM25, Overlap, Retriever, chunks_from
from clinic_desk.retrieval.dense import Dense, cached, openai_embedder
from clinic_desk.retrieval.evaluate import all_in_prompt_tokens, evaluate, load_questions, report

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
    args = parser.parse_args()
    if args.command == "eval-retrieval":
        asyncio.run(eval_retrieval(args.embeddings))
    if args.command == "chat":
        now = datetime.fromisoformat(args.now).replace(tzinfo=ZoneInfo("Europe/Madrid"))
        asyncio.run(chat(now, args.lang, args.trace))


if __name__ == "__main__":
    main()
