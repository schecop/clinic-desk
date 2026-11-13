"""The simulator end to end with scripted models: persona and receptionist both scripted, the real
graph and fake calendar in between. Checks the runner, the code checks and the persona cards."""

from __future__ import annotations

import asyncio
import json
from typing import Any
from zoneinfo import ZoneInfo

import pytest

from clinic_desk.clinic import load_clinic
from clinic_desk.llm.base import LLMResponse, ToolResults
from clinic_desk.llm.scripted import Request, ScriptedLLM, Step, call, say
from clinic_desk.simulator.checks import changes, check_calendar, claims_write, persona_broke
from clinic_desk.simulator.persona import CalendarExpect, load_personas, load_split, make_split
from clinic_desk.simulator.runner import RoleModel, persona_system, run_conversation, transcript

TZ = ZoneInfo("Europe/Madrid")


def persona(pid: str) -> Any:
    return next(p for p in load_personas(load_clinic()) if p.id == pid)


def first_afternoon_slot(req: Request) -> LLMResponse:
    for message in reversed(req.messages):
        if isinstance(message, ToolResults):
            slots = json.loads(message.results[0].content)["slots"]
            s = slots[0]
            return call(
                "propose_move", appointment_id="A003", staff_id=s["staff_id"], start=s["start"]
            )
    raise AssertionError("no slots")


CARMEN_DESK: list[Step] = [
    call("identify_patient", phone="600 000 002"),
    say(
        "Buenos días, Carmen. Tiene una revisión el miércoles 18 a las 9:30. ¿A qué día la cambio?"
    ),
    call(
        "find_slots",
        service_id="checkup",
        date_from="2026-11-16",
        date_to="2026-11-16",
        time_from="16:00",
    ),
    first_afternoon_slot,
    say("Hecho: su revisión queda cambiada al lunes 16 a las 16:00 con el Dr. Roca."),
    say("A usted, adiós."),  # the goodbye that carries [DONE] still reaches the desk
]
CARMEN_PATIENT: list[Step] = [
    say("Hola, buenos días. Quería cambiar mi revisión. Mi teléfono es el 600 000 002."),
    say("Al lunes 16 por la tarde, si puede ser, a partir de las cuatro."),
    say("Sí"),
    say("Muchas gracias, adiós. [DONE]"),
]


def run(pid: str, desk: list[Any], patient: list[Any]) -> Any:
    return asyncio.run(
        run_conversation(
            persona(pid),
            receptionist=RoleModel(ScriptedLLM(desk)),
            persona_model=RoleModel(ScriptedLLM(patient)),
        )
    )


def test_a_scripted_move_passes_every_code_check() -> None:
    c = run("carmen-move-afternoon", CARMEN_DESK, CARMEN_PATIENT)
    print("\n" + transcript(c))

    assert c.end == "done" and c.calendar_ok, c.calendar_why
    assert [t.writes for t in c.turns][2][0].startswith("moved A003")
    assert c.false_claims == 0 and c.claims_blocked == 0 and c.language_misses == 0
    assert c.code_pass
    assert [x.role for x in c.calls].count("persona") == 4


def test_a_claim_without_a_write_is_blocked_and_counted() -> None:
    # the D2 failure, replayed: the model says it moved the appointment, nothing was written.
    # The guard takes it back, so the patient is not lied to; the pass counts the block
    desk = [
        call("identify_patient", phone="600 000 002"),
        say("Ya está cambiada al lunes 16 a las 16:00."),
        say("De nada."),
    ]
    patient = [say("Cambie mi revisión al lunes por la tarde, 600 000 002."), say("Gracias [DONE]")]
    c = run("carmen-move-afternoon", desk, patient)
    print("\n" + transcript(c))

    assert c.false_claims == 0 and c.claims_blocked == 1
    assert "todavía no he hecho ningún cambio" in c.turns[0].desk
    assert c.calendar_ok is False and not c.code_pass


def test_a_spanish_reply_to_catalan_is_flagged_when_the_guard_cannot_fix_it() -> None:
    spanish = "Buenos días, ¿me puede dar su número de teléfono, por favor? Así busco su ficha."
    # the receptionist answers in Spanish; the guard's translation comes back Spanish too,
    # so the guard keeps the original and the simulator's language check must flag it
    desk = [say(spanish), say(spanish), say("Adéu.")]
    patient = [
        say("Bon dia, voldria anul·lar la cita de dilluns, sisplau."),
        say("No m'entén, adéu. [HANGUP]"),
    ]
    c = run("aina-cancel-extraction", desk, patient)
    print("\n" + transcript(c))

    assert c.end == "hangup"
    assert c.turns[0].lang_fix == "kept" and not c.turns[0].reply_lang_ok
    assert c.language_misses == 1 and not c.code_pass


def test_a_yes_said_together_with_the_end_marker_still_writes() -> None:
    # first real pass: "Sí, estoy de acuerdo. Muchas gracias, adiós. [DONE]" never reached the
    # receptionist, so the move was never written and the receptionist was blamed for it
    patient = [*CARMEN_PATIENT[:2], say("Sí, estoy de acuerdo. Muchas gracias, adiós. [DONE]")]
    desk = [*CARMEN_DESK[:4], say("Hecho, queda cambiada. Adiós.")]
    c = run("carmen-move-afternoon", desk, patient)

    assert c.end == "done" and c.calendar_ok, c.calendar_why
    assert c.turns[-1].writes and c.turns[-1].desk


def test_an_empty_persona_line_is_retried_once() -> None:
    # real pass: Gemma 4 spent all its tokens reasoning and said nothing
    empty = LLMResponse(say("").message, stop="max_tokens")
    c = run("minors-alone", [say("Bon dia.")], [empty, say("Bon dia, una pregunta. [DONE]")])
    assert c.end == "done" and c.turns[0].patient == "Bon dia, una pregunta."
    c = run("minors-alone", [], [empty, empty])
    assert c.end == "simulator_failure"


def test_a_persona_that_breaks_character_is_a_simulator_failure() -> None:
    c = run(
        "minors-alone",
        [],
        [say("Como modelo de lenguaje, no puedo interpretar a un paciente.")],
    )
    assert c.end == "simulator_failure" and not c.counted


def test_the_turn_cap_ends_the_call() -> None:
    p = persona("minors-alone")
    talk = [say("Bon dia, la meva filla pot venir sola?") for _ in range(p.max_turns)]
    replies = [say("Bon dia, li ho miro.") for _ in range(p.max_turns)]
    c = run("minors-alone", replies, talk)
    assert c.end == "turn_cap" and len(c.turns) == p.max_turns


# --- checks --------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("reply", "claims"),
    [
        ("Hecho: su revisión queda cambiada al lunes 16.", True),
        ("Ya está reservada su cita para el martes.", True),
        ("He anulado su cita del lunes.", True),
        ("Fet, la cita ha quedat anul·lada.", True),
        ("Ja està reservada.", True),
        ("Para confirmar: cambio su cita del miércoles al lunes. ¿Está de acuerdo?", False),
        ("Tengo libre el lunes a las 16:00 con el Dr. Roca.", False),
        ("¿Quiere que la anule?", False),
    ],
)
def test_claims_write(reply: str, claims: bool) -> None:
    assert claims_write(reply) is claims


@pytest.mark.parametrize(
    ("text", "broke"),
    [
        ("Como IA, no puedo fingir dolor.", True),
        ("I'm sorry, but I can't continue.", True),
        ("", True),
        ("Me duele mucho, ¿qué hago?", False),
        ("No puc venir dilluns.", False),
    ],
)
def test_persona_broke(text: str, broke: bool) -> None:
    assert persona_broke(text) is broke


def test_calendar_check_rejects_extra_writes() -> None:
    from clinic_desk.scheduling.fake import load_seed

    clinic = load_clinic()
    _, store = load_seed(clinic)
    before = store.snapshot()
    a = before["A001"]
    after = {**before, "A001": a.model_copy(update={"status": "cancelled"})}
    ok, _ = check_calendar(CalendarExpect(cancelled="A001"), before, after, "p05", TZ)
    assert ok
    b = before["A002"]
    after["A002"] = b.model_copy(update={"status": "cancelled"})
    ok, why = check_calendar(CalendarExpect(cancelled="A001"), before, after, "p05", TZ)
    assert not ok and "exactly one" in why
    assert changes(before, before) == []
    ok, _ = check_calendar(CalendarExpect(unchanged=True), before, before, None, TZ)
    assert ok


# --- cards and split -----------------------------------------------------------------------


def test_cards_load_and_the_split_is_fixed() -> None:
    personas = load_personas(load_clinic())
    assert len(personas) == 20
    split = load_split()
    assert split == make_split(personas), "split.yaml must match the seeded, stratified split"
    ids = {p.id for p in personas}
    assert set(split["visible"]) | set(split["held_out"]) == ids
    assert not set(split["visible"]) & set(split["held_out"])
    by_id = {p.id: p.category for p in personas}
    held_categories = [by_id[i] for i in split["held_out"]]
    assert len(held_categories) == len(set(held_categories)) == 6  # one per category with 2+


def test_persona_prompt_has_the_card_and_the_language_plan() -> None:
    system = persona_system(persona("joan-switch-checkup"))
    assert "600 000 001" in system and "{card}" not in system
    assert "first 2 turns, then switch to Catalan" in system
