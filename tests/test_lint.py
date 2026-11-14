"""The lint: each rule fires on the phrasing it targets and not on its correct form, and a hit
fails the criterion even when the judge passed it."""

from __future__ import annotations

from clinic_desk.simulator.judge import CriterionVerdict, Verdict
from clinic_desk.simulator.lint import lint, merge
from clinic_desk.simulator.runner import Conversation, Turn


def conv(*desk: str, write_at: int | None = None) -> Conversation:
    turns = [
        Turn(n=i, patient="…", desk=d, writes=["new A006"] if i == write_at else [])
        for i, d in enumerate(desk, start=1)
    ]
    return Conversation(persona_id="p", category="book", end="done", turns=turns)


def rules(c: Conversation) -> set[str]:
    return {h.rule for h in lint(c)}


def test_each_rule_fires_on_its_target() -> None:
    hits = lint(
        conv(
            "Bon dia, with pleasure. Em pot dir el seu telèfon?",
            "Tinc:\n1. Dilluns a les 10:00\n2. Dimarts a les 11:00",
            "Buenos días, con gusto le ayudo.",
            "Hi ha alguna cosa més en què li pugui ajudar? Li recomano arribar 10 minuts abans.",
        )
    )
    got = {(h.turn, h.criterion, h.rule) for h in hits}
    assert (1, "language", "English word") in got
    assert (1, "tone", "ca: em pot + verb (use em podria)") in got
    assert (2, "format", "numbered list") in got
    assert (3, "language", "es: con gusto (Latin American)") in got
    assert (4, "language", "ca: li + ajudar/registrar") in got
    assert (4, "tone", "reminder worded as a recommendation") in got


def test_the_correct_forms_pass() -> None:
    c = conv(
        "Bon dia, amb molt de gust. Em podria dir el seu telèfon?",
        "Tinc dilluns a les 10:00 o dimarts a les 11:00. Quina li va millor?",
        "Li he creat la fitxa. Recordi arribar 10 minuts abans. En què la puc ajudar?",
        "Queda reservada. Hi ha res més?",
        write_at=4,
    )
    assert lint(c) == []


def test_a_booking_closed_without_anything_else_fails_tone() -> None:
    closed = conv("Per confirmar…", "Queda reservada. Adéu!", write_at=2)
    assert "closes a booking without asking 'anything else?'" in rules(closed)
    offered = conv("Ja està anul·lada. Vol que li busqui una altra hora?", "Adéu!", write_at=1)
    assert not any("closes" in r for r in rules(offered))
    assert not any("closes" in r for r in rules(conv("No obrim dissabte.", "Adéu!")))


def test_a_lint_hit_fails_the_criterion_the_judge_passed() -> None:
    v = Verdict(
        conversation="x",
        model="m",
        criteria=[
            CriterionVerdict(id="format", applies=True, passed=True, reason="brief"),
            CriterionVerdict(id="tone", applies=True, passed=True),
        ],
    )
    merged = merge(v, lint(conv("Tinc:\n- dilluns\n- dimarts")))
    assert not merged.passed
    assert merged.by_id()["format"].label == "no" and merged.by_id()["tone"].label == "yes"
    assert "judge passed it: brief" in merged.by_id()["format"].reason
    assert merged.lint and v.passed  # the judge's own verdict is untouched
