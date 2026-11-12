"""One simulated call: a persona LLM talks to the real receptionist graph on a fresh fake calendar.

    greeting ─► persona ─► receptionist ─► persona ─► … until [DONE], [HANGUP], the turn cap,
                                                       a broken persona, or an error

From the persona's side the receptionist's lines are the "user" turns and its own lines are the
"assistant" turns. Every model call goes through `Metered`, so the conversation carries its own
cost and latency. Code checks (calendar, false claims, language, persona health) run per turn
and at the end; the judge (later) only gets what code cannot check.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Literal
from zoneinfo import ZoneInfo

from pydantic import BaseModel

from clinic_desk.clinic import Lang
from clinic_desk.llm.base import LLMClient, Message, UserMessage
from clinic_desk.llm.metered import Call, Metered
from clinic_desk.receptionist.graph import Receptionist, make_desk
from clinic_desk.receptionist.language import clear_lang
from clinic_desk.retrieval.base import Retriever
from clinic_desk.simulator.checks import (
    changes,
    check_calendar,
    claims_write,
    persona_broke,
    reply_language_ok,
)
from clinic_desk.simulator.persona import Persona

PERSONA_PROMPT = Path("prompts/persona.md")
DEFAULT_NOW = datetime(2026, 11, 11, 10, 0, tzinfo=ZoneInfo("Europe/Madrid"))
GREETING = "Clínica Dental Arrel, bon dia."
_END = re.compile(r"\[(DONE|HANGUP)\]")
_LANG_RULE: dict[Lang, str] = {"es": "Spanish", "ca": "Catalan"}

End = Literal["done", "hangup", "turn_cap", "simulator_failure", "error"]


@dataclass(frozen=True)
class RoleModel:
    """A client and its list prices, for one role."""

    client: LLMClient
    usd_per_mtok_in: Decimal = Decimal(0)
    usd_per_mtok_out: Decimal = Decimal(0)


class Turn(BaseModel):
    n: int
    patient: str
    desk: str = ""
    seconds: float = 0.0  # receptionist time for this turn
    writes: list[str] = []  # calendar changes made during this turn
    lang_fix: str | None = None
    false_claim: bool = False  # says done, and nothing has been written so far
    reply_lang_ok: bool = True
    persona_lang_ok: bool = True


class Conversation(BaseModel):
    persona_id: str
    category: str
    repeat: int = 0
    end: End
    turns: list[Turn]
    calendar_ok: bool | None = None  # None: not checked by this persona
    calendar_why: str = ""
    error: str | None = None
    calls: list[Call] = []

    @property
    def usd(self) -> Decimal:
        return sum((c.usd for c in self.calls), Decimal(0))

    @property
    def false_claims(self) -> int:
        return sum(t.false_claim for t in self.turns)

    @property
    def language_misses(self) -> int:
        return sum(not t.reply_lang_ok for t in self.turns)

    @property
    def counted(self) -> bool:
        """Simulator failures and errors are left out of the score (and reported apart)."""
        return self.end not in ("simulator_failure", "error")

    @property
    def code_pass(self) -> bool:
        """Everything code checks: task done in the calendar, no false claim, right language."""
        return (
            self.end == "done"
            and self.calendar_ok is not False
            and self.false_claims == 0
            and self.language_misses == 0
        )


def persona_system(persona: Persona) -> str:
    card = {
        "name": persona.caller.name,
        "phone": persona.caller.phone,
        "goal": persona.goal,
        "traits": ", ".join(persona.traits) or "-",
        "held back": persona.hidden or "-",
    }
    lines = "\n".join(f"- {k}: {v}" for k, v in card.items())
    plan = persona.language
    if plan.mixes:
        rule = (
            f"Speak {_LANG_RULE[plan.start]} mixed with the other language (Spanish/Catalan)"
            " inside your sentences, as some people in Barcelona do."
        )
    elif plan.switch_to and plan.at_turn:
        rule = (
            f"Speak {_LANG_RULE[plan.start]} for your first {plan.at_turn - 1} turns, then switch"
            f" to {_LANG_RULE[plan.switch_to]} for the rest of the call, without explaining why."
        )
    else:
        rule = f"Speak only {_LANG_RULE[plan.start]}, whatever language the receptionist uses."
    template = PERSONA_PROMPT.read_text(encoding="utf-8")
    return template.replace("{card}", lines).replace("{language_rule}", rule)


async def run_conversation(
    persona: Persona,
    *,
    receptionist: RoleModel,
    persona_model: RoleModel,
    translator: RoleModel | None = None,
    prompt: str | None = None,
    policies: Literal["prompt", "search"] = "prompt",
    retriever: Retriever | None = None,
    repeat: int = 0,
) -> Conversation:
    calls: list[Call] = []

    def meter(role: str, m: RoleModel) -> Metered:
        return Metered(m.client, role, m.usd_per_mtok_in, m.usd_per_mtok_out, calls)

    now = persona.now or DEFAULT_NOW
    desk = make_desk(
        meter("receptionist", receptionist),
        lambda: now,
        prompt=prompt,
        translator=meter("translator", translator) if translator else None,
        policies=policies,
        retriever=retriever,
    )
    assert desk.store is not None
    tz = ZoneInfo(desk.clinic.timezone)
    before = desk.store.snapshot()
    desk_side = Receptionist(desk, persona.language.start)
    patient = meter("persona", persona_model)
    system = persona_system(persona)
    history: list[Message] = [UserMessage(GREETING)]
    turns: list[Turn] = []
    end: End = "turn_cap"
    error: str | None = None
    written_so_far = False

    for n in range(1, persona.max_turns + 1):
        response = await patient.complete(system=system, messages=history, max_tokens=200)
        said = response.text.strip()
        marker = _END.search(said)
        said = _END.sub("", said).strip()
        if persona_broke(said) and not marker:
            turns.append(Turn(n=n, patient=said))
            end = "simulator_failure"
            break
        found = clear_lang(said)
        plan = persona.language
        persona_lang_ok = plan.mixes or found in (None, plan.expected(n))
        if marker:
            turns.append(Turn(n=n, patient=said, persona_lang_ok=persona_lang_ok))
            end = "done" if marker.group(1) == "DONE" else "hangup"
            break
        history.append(response.message)
        current = desk_side.state["lang"]
        snap = desk.store.snapshot()
        started = time.perf_counter()
        try:
            reply = await desk_side.reply(said)
        except Exception as exc:  # a crash is a finding, not a reason to lose the pass
            turns.append(Turn(n=n, patient=said, persona_lang_ok=persona_lang_ok))
            end, error = "error", f"{type(exc).__name__}: {exc}"
            break
        writes = changes(snap, desk.store.snapshot())
        written_so_far = written_so_far or bool(writes)
        lang_ok, _ = reply_language_ok(said, reply, current)
        turns.append(
            Turn(
                n=n,
                patient=said,
                desk=reply,
                seconds=round(time.perf_counter() - started, 2),
                writes=writes,
                lang_fix=desk_side.state["lang_fix"],
                false_claim=claims_write(reply) and not written_so_far,
                reply_lang_ok=plan.mixes or lang_ok,
                persona_lang_ok=persona_lang_ok,
            )
        )
        history.append(UserMessage(reply))

    patient_id = persona.caller.id
    if patient_id is None:
        found_patient = desk.patients.find_by_phone(persona.caller.phone)
        patient_id = found_patient.id if found_patient else None
    calendar_ok, why = check_calendar(
        persona.expect.calendar, before, desk.store.snapshot(), patient_id, tz
    )
    return Conversation(
        persona_id=persona.id,
        category=persona.category,
        repeat=repeat,
        end=end,
        turns=turns,
        calendar_ok=calendar_ok,
        calendar_why=why,
        error=error,
        calls=calls,
    )


def transcript(c: Conversation) -> str:
    """The conversation as text, with the code checks inline: for reading and for the judge."""
    lines = [f"# {c.persona_id} (repeat {c.repeat}) · end {c.end}", f"desk> {GREETING}"]
    for t in c.turns:
        lines.append(f"[{t.n}] patient> {t.patient}")
        if t.desk:
            flags = [
                *(f"WRITE {w}" for w in t.writes),
                *(["FALSE CLAIM"] if t.false_claim else []),
                *(["WRONG LANGUAGE"] if not t.reply_lang_ok else []),
                *([f"lang fix {t.lang_fix}"] if t.lang_fix else []),
            ]
            note = f"   ({'; '.join(flags)})" if flags else ""
            lines.append(f"[{t.n}] desk> {t.desk}{note}")
    lines.append(f"calendar: {c.calendar_ok} · {c.calendar_why}")
    if c.error:
        lines.append(f"error: {c.error}")
    return "\n".join(lines)
