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
from zoneinfo import ZoneInfo

from clinic_desk.config import load_config
from clinic_desk.env import load_env
from clinic_desk.llm.base import AssistantMessage, Message, ToolResults
from clinic_desk.llm.factory import build_llm
from clinic_desk.receptionist.graph import Receptionist, make_desk
from clinic_desk.receptionist.state import State

DEFAULT_NOW = "2026-11-11T10:00"


async def chat(now: datetime, lang: str, trace: bool) -> None:
    load_env()
    choice = load_config().llm["receptionist"]
    llm = build_llm(choice)
    desk = make_desk(llm, lambda: now)
    receptionist = Receptionist(desk, "ca" if lang == "ca" else "es")
    print(f"[{choice.provider}/{choice.model} · fake calendar · now {now:%a %Y-%m-%d %H:%M}]")
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
    print(f"\nupcoming in the fake calendar: {await _summary(receptionist)}")


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
    print(
        f"  [lang {state['lang']} · confirm {state['confirmation']} · pending {pending}"
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


def main() -> None:
    parser = argparse.ArgumentParser(prog="clinic-desk")
    sub = parser.add_subparsers(dest="command", required=True)
    chat_cmd = sub.add_parser("chat", help="chat with the receptionist (real model, fake calendar)")
    chat_cmd.add_argument("--now", default=DEFAULT_NOW, help="local time, YYYY-MM-DDTHH:MM")
    chat_cmd.add_argument("--lang", default="es", choices=["es", "ca"])
    chat_cmd.add_argument("--trace", action="store_true", help="print tool calls and results")
    args = parser.parse_args()
    if args.command == "chat":
        now = datetime.fromisoformat(args.now).replace(tzinfo=ZoneInfo("Europe/Madrid"))
        asyncio.run(chat(now, args.lang, args.trace))


if __name__ == "__main__":
    main()
