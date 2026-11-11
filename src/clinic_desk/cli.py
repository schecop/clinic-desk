"""`clinic-desk chat`: talk to the receptionist on the seeded fake calendar.

Uses the receptionist model from config (`llm.receptionist`), so it calls a real (paid) API.
The clock is fixed (default Wed 11 Nov 2026, 10:00) so the seeded appointments make sense.
"""

from __future__ import annotations

import argparse
import asyncio
from datetime import datetime
from zoneinfo import ZoneInfo

from clinic_desk.config import load_config
from clinic_desk.env import load_env
from clinic_desk.llm.factory import build_llm
from clinic_desk.receptionist.graph import Receptionist, make_desk

DEFAULT_NOW = "2026-11-11T10:00"


async def chat(now: datetime, lang: str) -> None:
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
            print(f"desk> {await receptionist.reply(text)}\n")
    except (KeyboardInterrupt, EOFError):
        pass
    finally:
        await llm.aclose()
    print(f"\nupcoming in the fake calendar: {await _summary(receptionist)}")


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
    args = parser.parse_args()
    if args.command == "chat":
        tz = ZoneInfo("Europe/Madrid")
        asyncio.run(chat(datetime.fromisoformat(args.now).replace(tzinfo=tz), args.lang))


if __name__ == "__main__":
    main()
