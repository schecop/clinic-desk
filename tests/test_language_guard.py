"""The language guard: a reply in the wrong language is translated by a second call, checked by
code, and the original kept if the translation changed a number. Scripted models only."""

from __future__ import annotations

import asyncio
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from clinic_desk.llm.base import AssistantMessage
from clinic_desk.llm.scripted import ScriptedLLM, say
from clinic_desk.receptionist.graph import Receptionist, make_desk
from clinic_desk.receptionist.language import clear_lang, guess_lang
from clinic_desk.receptionist.translate import needs_translation

NOW = datetime(2026, 11, 11, 10, 0, tzinfo=ZoneInfo("Europe/Madrid"))

QUESTION_CA = "I si al final no puc venir, què passa?"
ANSWER_ES = (
    "Si no puede venir, llame con 24 horas de antelación; si no, se cobran 20 € por la visita."
)
ANSWER_CA = "Si no pot venir, truqui amb 24 hores d'antelació; si no, es cobren 20 € per la visita."


def test_a_spanish_reply_to_a_catalan_patient_is_translated() -> None:
    receptionist_llm = ScriptedLLM([say(ANSWER_ES)])
    translator = ScriptedLLM([say(ANSWER_CA)])
    desk = make_desk(receptionist_llm, lambda: NOW, translator=translator)
    r = Receptionist(desk, "es")

    heard = asyncio.run(r.reply(QUESTION_CA))

    assert heard == ANSWER_CA
    assert r.state["lang"] == "ca" and r.state["lang_fix"] == "translated"
    # the translator saw only the reply, and was told the target language
    (request,) = translator.requests
    assert "Catalan" in request.system and request.tools == []
    assert [m.content for m in request.messages] == [ANSWER_ES]  # type: ignore[union-attr]
    # the history holds what the patient heard, without the provider's untranslated raw turn
    last = r.state["messages"][-1]
    assert isinstance(last, AssistantMessage) and last.text == ANSWER_CA and last.raw is None


def test_a_translation_that_changes_a_number_is_discarded() -> None:
    wrong = ANSWER_CA.replace("24 hores", "48 hores")
    desk = make_desk(
        ScriptedLLM([say(ANSWER_ES)]), lambda: NOW, translator=ScriptedLLM([say(wrong)])
    )
    r = Receptionist(desk, "ca")

    heard = asyncio.run(r.reply(QUESTION_CA))

    assert heard == ANSWER_ES  # the right fact in the wrong language, not a wrong fact
    assert r.state["lang_fix"] == "kept"


def test_a_translation_still_in_the_wrong_language_is_discarded() -> None:
    desk = make_desk(
        ScriptedLLM([say(ANSWER_ES)]), lambda: NOW, translator=ScriptedLLM([say(ANSWER_ES)])
    )
    r = Receptionist(desk, "ca")

    assert asyncio.run(r.reply(QUESTION_CA)) == ANSWER_ES
    assert r.state["lang_fix"] == "kept"


def test_a_reply_in_the_right_language_costs_no_extra_call() -> None:
    llm = ScriptedLLM([say(ANSWER_CA)])  # a translation call would exhaust the script
    r = Receptionist(make_desk(llm, lambda: NOW), "ca")

    assert asyncio.run(r.reply(QUESTION_CA)) == ANSWER_CA
    assert len(llm.requests) == 1 and r.state["lang_fix"] is None


def test_without_a_translator_the_receptionist_model_translates() -> None:
    llm = ScriptedLLM([say(ANSWER_ES), say(ANSWER_CA)])
    r = Receptionist(make_desk(llm, lambda: NOW), "ca")

    assert asyncio.run(r.reply(QUESTION_CA)) == ANSWER_CA
    assert len(llm.requests) == 2 and llm.requests[1].tools == []


def test_the_flag_resets_on_the_next_turn() -> None:
    llm = ScriptedLLM([say(ANSWER_ES), say(ANSWER_CA), say("D'acord, fins aviat.")])
    r = Receptionist(make_desk(llm, lambda: NOW), "ca")
    asyncio.run(r.reply(QUESTION_CA))
    asyncio.run(r.reply("Moltes gràcies"))
    assert r.state["lang_fix"] is None


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        (ANSWER_ES, "es"),
        (ANSWER_CA, "ca"),
        # shared words ("el", "es", "una", "del") used to count as Spanish
        ("Li puc oferir el dilluns 16 a les 9; es pot fer una visita del matí.", "ca"),
        ("Perfecte.", None),  # too little evidence: no translation
        ("Sí, a las 10.", None),
    ],
)
def test_clear_lang(text: str, expected: str | None) -> None:
    assert clear_lang(text) == expected


def test_needs_translation_only_for_the_other_language() -> None:
    assert needs_translation(ANSWER_ES, "ca")
    assert not needs_translation(ANSWER_ES, "es")
    assert not needs_translation("D'acord.", "es")


def test_a_catalan_sentence_with_shared_words_is_read_as_catalan() -> None:
    assert guess_lang("el dilluns es pot?", default="es") == "ca"


def test_a_catalan_turn_with_cita_and_an_elision_stays_catalan() -> None:
    """calib3 root-canal: 'cita' counted as Spanish flipped the call, and the guard then
    'translated' two correct Catalan replies into Spanish."""
    turn = "I si demano una cita igualment, m\u2019ho podran mirar allà?"
    assert guess_lang(turn, default="es") == "ca"
    assert guess_lang("Quería pedir una cita para mañana", default="ca") == "es"
