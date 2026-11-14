"""The real receptionist graph, driven by a scripted model: no key, no network.

The model is scripted to misbehave on purpose in several tests (book without asking, claim it
booked, try a write tool it does not have). The calendar must stay untouched in all of them:
that is the confirmation gate working in code, not in the prompt.
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

import pytest

from clinic_desk.llm.base import LLMResponse, ToolResults
from clinic_desk.llm.scripted import Request, ScriptedLLM, call, calls, say
from clinic_desk.receptionist.graph import Receptionist, make_desk
from clinic_desk.receptionist.language import classify_confirmation, guess_lang, of
from clinic_desk.receptionist.state import Desk
from clinic_desk.receptionist.tools import TOOLS

TZ = ZoneInfo("Europe/Madrid")
NOW = datetime(2026, 11, 11, 10, 0, tzinfo=TZ)


def desk_with(*steps: Any) -> tuple[Desk, ScriptedLLM]:
    llm = ScriptedLLM(steps)
    return make_desk(llm, lambda: NOW), llm


def chat(desk: Desk, lang: str = "es") -> Receptionist:
    return Receptionist(desk, lang)  # type: ignore[arg-type]


def last_tool_payload(request: Request) -> dict[str, Any]:
    results = request.messages[-1]
    assert isinstance(results, ToolResults)
    parsed: dict[str, Any] = json.loads(results.results[0].content)
    return parsed


def latest_slots(request: Request) -> list[dict[str, Any]]:
    """The slots of the most recent find_slots result the model was shown."""
    for message in reversed(request.messages):
        if isinstance(message, ToolResults):
            for result in message.results:
                if result.name == "find_slots":
                    slots: list[dict[str, Any]] = json.loads(result.content)["slots"]
                    return slots
    raise AssertionError("no find_slots result in the conversation")


def propose_first_slot(request: Request) -> LLMResponse:
    """Echo the first slot of the latest find_slots result into propose_booking."""
    s = latest_slots(request)[0]
    return call(
        "propose_booking", service_id=s["service_id"], staff_id=s["staff_id"], start=s["start"]
    )


async def upcoming(desk: Desk, patient_id: str) -> list[str]:
    return [
        f"{a.service_id}@{a.start:%m-%d %H:%M}" for a in await desk.scheduler.upcoming(patient_id)
    ]


FILLING = {"service_id": "filling", "date_from": "2026-11-16", "date_to": "2026-11-20"}


def test_a_booking_is_written_only_after_the_patient_says_yes() -> None:
    desk, llm = desk_with(
        call("identify_patient", phone="600 000 002"),
        call("find_slots", **FILLING),
        say("Tengo el lunes 16 a las 9:00 con el Dr. Roca. ¿Le va bien?"),
        propose_first_slot,
        say("Hecho, le esperamos el lunes."),
    )
    r = chat(desk)

    async def run() -> None:
        assert "lunes 16" in await r.reply("Hola, soy Carmen, 600 000 002. Necesito un empaste.")
        question = await r.reply("Sí, el lunes a las nueve.")
        assert question.startswith(
            "Para confirmar: cita de empaste con el Dr. Pau Roca"
            " el lunes 16 de noviembre a las 09:00"
        )
        assert await upcoming(desk, "p02") == ["checkup@11-18 09:30"]  # nothing written yet
        assert await r.reply("Sí, perfecto.") == "Hecho, le esperamos el lunes."
        assert await upcoming(desk, "p02") == ["filling@11-16 09:00", "checkup@11-18 09:30"]

    asyncio.run(run())
    assert llm.remaining == 0
    assert "Booked A006: filling with Dr. Pau Roca" in llm.requests[-1].system


@pytest.mark.parametrize(
    "answer", ["No, mejor por la tarde.", "Sí, pero mejor a las 11", "¿Cuánto cuesta?"]
)
def test_anything_but_a_plain_yes_writes_nothing(answer: str) -> None:
    desk, _ = desk_with(
        call("identify_patient", phone="600000002"),
        call("find_slots", **FILLING),
        propose_first_slot,
        say("De acuerdo."),
    )
    r = chat(desk)

    async def run() -> None:
        await r.reply("Soy Carmen, 600000002, un empaste el lunes.")
        await r.reply(answer)
        assert await upcoming(desk, "p02") == ["checkup@11-18 09:30"]

    asyncio.run(run())
    assert r.state["confirmation"] in ("no", "unclear")


def test_a_no_drops_the_proposal_and_tells_the_model() -> None:
    desk, llm = desk_with(
        call("identify_patient", phone="600000002"),
        call("find_slots", **FILLING),
        propose_first_slot,
        say("De acuerdo, ¿qué otra hora le va bien?"),
    )
    r = chat(desk)
    asyncio.run(r.reply("Soy Carmen, 600000002, un empaste el lunes."))
    assert asyncio.run(r.reply("No, gracias.")) == "De acuerdo, ¿qué otra hora le va bien?"
    assert r.state["pending"] is None
    assert "did NOT confirm" in llm.requests[-1].system


def test_a_question_at_confirmation_keeps_the_proposal_and_the_graph_asks_again() -> None:
    """Regression (second real chat): "¿y eso cuánto me cuesta?" dropped the proposal; the
    model then asked "¿confirma?" itself and, on "Sí", claimed a move that never happened."""
    desk, llm = desk_with(
        call("identify_patient", phone="600000002"),
        call("find_slots", **FILLING),
        propose_first_slot,
        call("clinic_info"),  # a read tool while the proposal is pending: no early re-ask
        say("El empaste cuesta 70 €."),
        say("Hecho, le esperamos el lunes."),
    )
    r = chat(desk)

    async def run() -> None:
        await r.reply("Soy Carmen, 600000002, un empaste el lunes.")
        reply = await r.reply("¿Y eso cuánto me cuesta?")
        price, question = reply.split("\n")
        assert price == "El empaste cuesta 70 €."
        assert question.startswith("Para confirmar: cita de empaste con el Dr. Pau Roca")
        assert r.state["pending"] is not None
        assert await upcoming(desk, "p02") == ["checkup@11-18 09:30"]
        await r.reply("Sí.")
        assert await upcoming(desk, "p02") == ["filling@11-16 09:00", "checkup@11-18 09:30"]

    asyncio.run(run())
    assert "Pending proposal, NOT confirmed and NOT written" in llm.requests[4].system


def test_an_unclear_reply_can_replace_the_proposal() -> None:
    desk, _ = desk_with(
        call("identify_patient", phone="600000002"),
        call("find_slots", **FILLING),
        propose_first_slot,
        lambda req: call(
            "propose_booking", service_id="filling", staff_id="roca", start="2026-11-16T11:00"
        ),
        say("Hecho."),
    )
    r = chat(desk)

    async def run() -> None:
        await r.reply("Soy Carmen, 600000002, un empaste el lunes.")
        question = await r.reply("Sí, pero mejor a las 11")
        assert "a las 11:00" in question and "a las 09:00" not in question
        await r.reply("Sí")
        assert await upcoming(desk, "p02") == ["filling@11-16 11:00", "checkup@11-18 09:30"]

    asyncio.run(run())


def test_three_unclear_replies_in_a_row_drop_the_proposal() -> None:
    desk, _ = desk_with(
        call("identify_patient", phone="600000002"),
        call("find_slots", **FILLING),
        propose_first_slot,
        say("Es un empaste."),
        say("Dura 45 minutos."),
        say("Vale, lo dejamos. ¿Algo más?"),
    )
    r = chat(desk)

    async def run() -> None:
        await r.reply("Soy Carmen, 600000002, un empaste el lunes.")
        assert "Para confirmar" in await r.reply("¿Qué es exactamente?")
        assert "Para confirmar" in await r.reply("¿Y cuánto dura?")
        assert await r.reply("Bueno...") == "Vale, lo dejamos. ¿Algo más?"

    asyncio.run(run())
    assert r.state["pending"] is None and r.state["outcome"] is not None
    assert r.state["outcome"].result == "declined"


def test_text_next_to_a_tool_call_is_not_spoken() -> None:
    desk, _ = desk_with(
        call("identify_patient", text="Voy a buscar su ficha.", phone="600000002"),
        say("Hola, Carmen. ¿En qué le ayudo?"),
    )
    assert (
        asyncio.run(chat(desk).reply("Soy Carmen, 600000002")) == "Hola, Carmen. ¿En qué le ayudo?"
    )


def test_the_model_has_no_write_tool_and_saying_booked_books_nothing() -> None:
    desk, _ = desk_with(
        call("identify_patient", phone="600000002"),
        call("book_appointment", service_id="filling", staff_id="roca", start="2026-11-16T09:00"),
        say("¡Reservado!"),  # the model lies; the calendar must not change
    )
    r = chat(desk)
    asyncio.run(r.reply("Soy Carmen, 600000002, resérveme un empaste el lunes a las 9."))
    assert not any(name.startswith(("book", "move", "cancel")) for name in TOOLS)
    assert asyncio.run(upcoming(desk, "p02")) == ["checkup@11-18 09:30"]
    tool_results = r.state["messages"][-2]
    assert isinstance(tool_results, ToolResults) and tool_results.results[0].is_error


def test_a_claimed_write_while_the_proposal_is_pending_is_not_spoken() -> None:
    """Regression (real pass, Aina): digits in the yes made it unclear, so nothing was written,
    and Haiku said "queda anul·lada" anyway. The guard drops that reply; the question repeats."""
    desk, llm = desk_with(
        call("identify_patient", phone="600 000 005"),
        call("propose_cancel", appointment_id="A001"),
        say("Perfecte, Aina. La seva cita del dilluns 16 de novembre queda anul·lada."),
        say("Ha quedat anul·lada la seva cita. Que tingui un bon dia."),
    )
    r = chat(desk, "ca")

    async def run() -> None:
        question = await r.reply("Sóc l'Aina, 600 000 005. Vull anul·lar la cita de dilluns.")
        reply = await r.reply("El meu número és el 600 00 00 00 5. I sí, estic d'acord.")
        assert reply == question  # only the graph's question: the false "done" is not heard
        assert r.state["claim_blocked"] and r.state["pending"] is not None
        assert not any("queda anul·lada" in getattr(m, "text", "") for m in r.state["messages"])
        assert await upcoming(desk, "p05") != []
        reply = await r.reply("Sí.")
        assert reply.startswith("Ha quedat anul·lada")  # after the write, "done" is true
        assert not r.state["claim_blocked"]
        assert await upcoming(desk, "p05") == []

    asyncio.run(run())
    assert len(llm.requests) == 4


def test_a_claimed_write_with_nothing_proposed_is_taken_back() -> None:
    desk, _ = desk_with(
        call("identify_patient", phone="600000002"),
        say("Ya está cambiada al lunes 16 a las 16:00."),
    )
    r = chat(desk)
    reply = asyncio.run(r.reply("Soy Carmen, 600000002, cámbieme la revisión al lunes."))
    assert reply.startswith("Perdone, me he expresado mal: todavía no he hecho ningún cambio")
    assert r.state["claim_blocked"]
    assert asyncio.run(upcoming(desk, "p02")) == ["checkup@11-18 09:30"]


def test_an_earlier_write_does_not_license_a_claim_about_a_pending_one() -> None:
    desk, _ = desk_with(
        call("identify_patient", phone="600 000 001"),
        call("find_slots", service_id="cleaning", date_from="2026-11-18", date_to="2026-11-18"),
        lambda req: call(
            "propose_move",
            appointment_id="A002",
            staff_id="soler",
            start=latest_slots(req)[0]["start"],
        ),
        say("Fet, l'he canviada."),
        call("propose_cancel", appointment_id="A002"),
        say("Cap problema: queda anul·lada."),  # the move was written; the cancel was not
    )
    r = chat(desk, "ca")

    async def run() -> None:
        await r.reply("Sóc en Joan, el 600 000 001. Vull canviar la neteja al dimecres.")
        await r.reply("D'acord.")
        question = await r.reply("De fet, millor anul·leu-la.")
        reply = await r.reply("I em tornen els diners?")
        assert reply == question and r.state["claim_blocked"]
        assert await upcoming(desk, "p01") == ["cleaning@11-18 09:00"]

    asyncio.run(run())


def test_no_search_or_proposal_before_the_patient_is_identified() -> None:
    desk, llm = desk_with(
        call("find_slots", **FILLING),
        say("¿Me dice su teléfono?"),
    )
    asyncio.run(chat(desk).reply("Quiero un empaste el lunes."))
    assert "identify_patient_first" in llm.requests[1].messages[-1].results[0].content  # type: ignore[union-attr]


def test_a_patient_cannot_cancel_someone_elses_appointment() -> None:
    desk, llm = desk_with(
        call("identify_patient", phone="600000001"),  # Joan
        call("propose_cancel", appointment_id="A003"),  # Carmen's
        say("Esa cita no es suya."),
    )
    r = chat(desk)
    asyncio.run(r.reply("Soc en Joan, 600000001, anul·leu la cita A003."))
    assert "not_this_patients_appointment" in json.dumps(last_tool_payload(llm.requests[-1]))
    assert r.state["pending"] is None


def test_a_slot_taken_between_proposal_and_yes_fails_safely() -> None:
    desk, _ = desk_with(
        call("identify_patient", phone="600000002"),
        call("find_slots", **FILLING),
        propose_first_slot,
        say("Lo siento, ese hueco se acaba de ocupar. ¿Le busco otro?"),
    )
    r = chat(desk)

    async def run() -> None:
        await r.reply("Soy Carmen, 600000002, un empaste el lunes.")
        # someone else takes Roca's Monday 09:00 while Carmen thinks it over
        pending = r.state["pending"]
        assert pending is not None and pending.slot is not None
        await desk.scheduler.book("p04", pending.slot.model_copy(update={"service_id": "checkup"}))
        await r.reply("Sí.")
        assert await upcoming(desk, "p02") == ["checkup@11-18 09:30"]

    asyncio.run(run())
    assert r.state["outcome"] is not None and r.state["outcome"].problem == "staff_busy"


def test_moving_and_cancelling_go_through_the_same_gate_in_catalan() -> None:
    desk, _ = desk_with(
        call("identify_patient", phone="600 000 001"),
        call("my_appointments"),
        call("find_slots", service_id="cleaning", date_from="2026-11-18", date_to="2026-11-18"),
        lambda req: call(
            "propose_move",
            appointment_id="A002",
            staff_id="soler",
            start=latest_slots(req)[0]["start"],
        ),
        say("Fet, l'he canviada."),
        call("propose_cancel", appointment_id="A002"),
        say("Anul·lada."),
    )
    r = chat(desk, "ca")

    async def run() -> None:
        question = await r.reply(
            "Bon dia, sóc en Joan, el 600 000 001. Vull canviar la neteja al dimecres."
        )
        assert question.startswith(
            "Per confirmar: canvio la seva cita de neteja dental del dimarts 17 de novembre"
            " a les 10:00 al dimecres 18 de novembre a les 09:00, amb Laia Soler."
        )
        await r.reply("D'acord.")
        assert await upcoming(desk, "p01") == ["cleaning@11-18 09:00"]
        question = await r.reply("De fet, millor anul·leu-la.")
        assert question.startswith(
            "Per confirmar: anul·lo la seva cita de neteja dental amb Laia Soler del dimecres 18"
        )
        await r.reply("Sí, gràcies.")
        assert await upcoming(desk, "p01") == []

    asyncio.run(run())


def test_a_runaway_tool_loop_hands_off_to_a_human() -> None:
    desk, llm = desk_with(*[call("clinic_info") for _ in range(6)])
    reply = asyncio.run(chat(desk).reply("¿A qué hora abrís?"))
    assert "llame al +34 900 000 000" in reply and llm.remaining == 0


def test_the_context_block_gives_today_and_named_days() -> None:
    desk, llm = desk_with(say("Bon dia!"))
    asyncio.run(chat(desk, "ca").reply("Bon dia"))
    system = llm.requests[0].system
    assert "Today is 2026-11-11" in system
    assert "2026-11-16 dilluns 16 de novembre" in system
    assert "2026-12-08" not in system  # beyond 14 days
    assert system.rstrip().splitlines()[-1].startswith("- Reply language: Catalan.")


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Sí", "yes"),
        ("sí, perfecto", "yes"),
        ("Vale, gracias", "yes"),
        ("Pues sí", "yes"),
        ("D'acord", "yes"),
        ("Perfecte, moltes gràcies", "yes"),
        ("Sí, sí, de acuerdo", "yes"),
        ("No", "no"),
        ("no, gracias", "no"),
        ("Mejor no", "no"),
        ("Sí, pero mejor a las 11", "unclear"),
        ("si puede ser por la tarde", "unclear"),
        ("¿Y cuánto cuesta?", "unclear"),
        ("Bueno...", "unclear"),
        ("hmm", "unclear"),
        ("Sí a las 10", "unclear"),
        ("Potser sí", "unclear"),
        ("", "unclear"),
    ],
)
def test_confirmation_classifier(text: str, expected: str) -> None:
    assert classify_confirmation(text) == expected


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Hola, quiero una cita para el lunes", "es"),
        ("Bon dia, vull una cita per dilluns", "ca"),
        ("Moltes gràcies", "ca"),
        ("ok", "es"),  # too short: keeps the current language
    ],
)
def test_guess_lang(text: str, expected: str) -> None:
    assert guess_lang(text, default="es") == expected


def test_catalan_elides_de_before_a_vowel() -> None:
    # first real pass read "la seva cita de extracció simple"
    assert of("extracció simple", "ca") == "d'extracció simple"
    assert of("higiene", "ca") == "d'higiene"
    assert of("revisió", "ca") == "de revisió"
    assert of("extracción simple", "es") == "de extracción simple"


def test_tool_specs_are_plain_json_schema() -> None:
    for tool in TOOLS.values():
        schema = tool.spec.input_schema
        assert schema["type"] == "object" and "title" not in schema
        json.dumps(schema)
    assert set(TOOLS["find_slots"].spec.input_schema["required"]) == {
        "service_id",
        "date_from",
        "date_to",
    }


def test_find_slots_says_when_it_truncates_and_can_search_afternoons() -> None:
    """Regression (first real chat): the tool returned Roca's first 6 Monday slots, all in the
    morning, and the model told the patient there was nothing in the afternoon."""
    monday = {"service_id": "checkup", "date_from": "2026-11-16", "date_to": "2026-11-16"}
    desk, llm = desk_with(
        call("identify_patient", phone="600000002"),
        call("find_slots", **monday),
        call("find_slots", **monday, time_from="16:00"),
        say("Tengo el lunes a las 16:00."),
    )
    asyncio.run(chat(desk).reply("Soy Carmen, 600000002. La revisión, el lunes por la tarde."))
    whole_day = last_tool_payload(llm.requests[2])
    assert whole_day["total_free"] > len(whole_day["slots"]) == 6
    assert "Showing the earliest 6 of" in whole_day["note"]
    afternoon = last_tool_payload(llm.requests[3])
    assert [s["start"][-5:] for s in afternoon["slots"][:2]] == ["16:00", "16:15"]
    assert {s["staff_id"] for s in afternoon["slots"]} == {"roca"}  # usual dentist kept


@pytest.mark.parametrize("tool", ["identify_patient", "register_patient"])
def test_a_misheard_phone_number_is_refused_not_registered(tool: str) -> None:
    """calib3: '600 000 02' opened a new record, and Carmen's real appointment was lost."""
    args = {"phone": "600 000 02"} | (
        {"name": "Carmen López"} if tool == "register_patient" else {}
    )
    desk, llm = desk_with(calls((tool, args)), say("¿Me lo puede repetir?"))
    asyncio.run(chat(desk).reply("Soy Carmen López, el 600 000 02."))
    payload = last_tool_payload(llm.requests[-1])
    assert payload["error"] == "invalid_phone" and "has 8" in payload["detail"]
    assert desk.patients.find_by_phone("60000002") is None  # no record opened


def test_a_new_patient_is_told_the_services_from_the_clinic_data() -> None:
    """calib3 pedro: the tool said 'first visit or emergency only', so a cleaning was refused."""
    desk, llm = desk_with(
        calls(("register_patient", {"name": "Pedro Navarro", "phone": "600 000 099"})),
        say("Ja l'he registrat. Què necessita?"),
    )
    asyncio.run(chat(desk, "ca").reply("Em dic Pedro Navarro, el 600 000 099."))
    payload = last_tool_payload(llm.requests[1])
    assert payload["bookable_now"] == ["primera visita", "neteja dental", "urgència dental"]


def test_the_confirmation_question_is_heard_once_when_the_model_copies_it() -> None:
    """calib3 aina-late-cancel r0: the model repeated the question and the graph added it again."""
    question: list[str] = []

    def answer_and_copy(request: Request) -> LLMResponse:
        return say("El empaste cuesta 70 €.\n" + question[0])

    desk, _ = desk_with(
        call("identify_patient", phone="600000002"),
        call("find_slots", **FILLING),
        propose_first_slot,
        answer_and_copy,
    )
    r = chat(desk)

    async def run() -> None:
        await r.reply("Soy Carmen, 600000002, un empaste el lunes.")
        question.append(r.state["replies"][-1])
        reply = await r.reply("¿Y eso cuánto me cuesta?")
        assert reply.count("Para confirmar:") == 1
        assert reply == "El empaste cuesta 70 €.\n" + question[0]

    asyncio.run(run())
