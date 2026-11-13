"""What code can check about a simulated conversation, so the judge does not have to.

- calendar: the fake calendar's end state matches the persona's `expect.calendar`;
- false claim: the receptionist says a booking/move/cancel is done while nothing was written
  (the worst failure seen in a real chat: the patient was told a move that never happened);
- language: each receptionist reply is in the language of the patient's message before it;
- simulator health: the persona did not break character (refusals, "as an AI") and spoke the
  card's language. A broken persona makes the conversation a simulator failure, out of the score.

Language and claims are read with word lists: cheap, and wrong sometimes. They flag; the
transcript and the judge decide.
"""

from __future__ import annotations

import re
from datetime import time
from zoneinfo import ZoneInfo

from clinic_desk.clinic import Lang
from clinic_desk.receptionist.claims import claims_write, plain
from clinic_desk.receptionist.language import clear_lang, guess_lang
from clinic_desk.scheduling.models import Appointment
from clinic_desk.simulator.persona import CalendarExpect, SlotMatch

__all__ = [
    "changes",
    "check_calendar",
    "claims_write",
    "persona_broke",
    "plain",
    "reply_language_ok",
]

Snapshot = dict[str, Appointment]


# --- calendar ------------------------------------------------------------------------------


def changes(before: Snapshot, after: Snapshot) -> list[str]:
    """Readable diff: 'new A006 …', 'moved A003 …', 'cancelled A001'."""
    out = []
    for aid, a in after.items():
        old = before.get(aid)
        if old is None:
            out.append(f"new {aid} {a.service_id} {a.staff_id} {a.start.isoformat()}")
        elif old.status != a.status:
            out.append(f"{a.status} {aid}")
        elif (old.start, old.staff_id) != (a.start, a.staff_id):
            out.append(f"moved {aid} {old.start.isoformat()} -> {a.start.isoformat()}")
    return out


def _matches(a: Appointment, m: SlotMatch, tz: ZoneInfo) -> bool:
    local = a.start.astimezone(tz)
    return (
        a.status == "booked"
        and a.service_id == m.service
        and (m.staff is None or a.staff_id == m.staff)
        and (m.weekday is None or local.strftime("%a").lower() == m.weekday)
        and (m.date is None or local.date().isoformat() == m.date)
        and (m.after is None or local.time() >= time.fromisoformat(m.after))
        and (m.before is None or local.time() < time.fromisoformat(m.before))
    )


def check_calendar(
    expect: CalendarExpect, before: Snapshot, after: Snapshot, patient_id: str | None, tz: ZoneInfo
) -> tuple[bool | None, str]:
    """(passed, why). None when the persona does not check the calendar (`any`)."""
    diff = changes(before, after)
    if expect.any:
        return None, f"not checked; changes: {diff or 'none'}"
    if expect.unchanged:
        return (not diff), ("no changes" if not diff else f"unexpected writes: {diff}")
    if len(diff) != 1:
        return False, f"expected exactly one write, got {diff or 'none'}"
    if expect.cancelled is not None:
        a = after.get(expect.cancelled)
        ok = a is not None and a.status == "cancelled" and a.patient_id == patient_id
        return ok, f"{'cancelled' if ok else 'wrong write'}: {diff[0]}"
    match = expect.booked or expect.moved
    assert match is not None
    for aid, a in after.items():
        old = before.get(aid)
        if a.patient_id != patient_id or not _matches(a, match, tz):
            continue
        if expect.booked and old is None:
            return True, f"booked: {diff[0]}"
        if expect.moved and old is not None and old.start != a.start:
            return True, f"moved: {diff[0]}"
    return False, f"write does not match {match.model_dump(exclude_none=True)}: {diff[0]}"


# --- what the receptionist claims: see receptionist.claims (shared with the claim guard)


# --- language ------------------------------------------------------------------------------


def reply_language_ok(patient: str, reply: str, current: Lang) -> tuple[bool, Lang]:
    """(ok, the language the reply should be in). Clear evidence only: unclear text passes."""
    expected = guess_lang(patient, default=current)
    found = clear_lang(reply)
    return found in (None, expected), expected


# --- simulator health ----------------------------------------------------------------------

_BROKE = re.compile(
    r"\b(?:como (?:una )?(?:ia|inteligencia artificial|modelo de lenguaje|asistente)"
    r"|com a (?:ia|intel.ligencia artificial|model de llenguatge|assistent)"
    r"|as an ai|language model|i can.?t (?:help|play|continue)|i.m sorry, but"
    r"|no puedo (?:interpretar|continuar|seguir con) (?:este|esta|el) (?:papel|rol|simulacion)"
    r"|no puc (?:interpretar|continuar))"
)


def persona_broke(text: str) -> bool:
    return not text.strip() or bool(_BROKE.search(plain(text)))
