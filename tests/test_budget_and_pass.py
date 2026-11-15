"""The budget guard (ledger, estimate, refusal) and a whole simulator pass on scripted models."""

from __future__ import annotations

import asyncio
import json
from decimal import Decimal
from pathlib import Path

import pytest

from clinic_desk.clinic import load_clinic
from clinic_desk.llm.base import LLMResponse
from clinic_desk.llm.metered import Call
from clinic_desk.llm.scripted import Request, ScriptedLLM, say
from clinic_desk.simulator import budget
from clinic_desk.simulator.persona import load_personas
from clinic_desk.simulator.runner import RoleModel
from clinic_desk.simulator.simulate import run_pass


def paid(usd: str, role: str = "receptionist") -> Call:
    return Call(role, "anthropic", "m", 1000, 100, Decimal(usd), 10.0, "end")


def test_the_ledger_keeps_paid_calls_only_and_sums_them(tmp_path: Path) -> None:
    ledger = tmp_path / "cost.jsonl"
    free = Call("persona", "scripted", "s", 10, 10, Decimal(0), 1.0, "end")
    assert budget.record([paid("0.01"), free, paid("0.02")], "run1", ledger) == Decimal("0.03")
    assert len(ledger.read_text("utf-8").splitlines()) == 2
    assert budget.spent(ledger) == Decimal("0.03")
    assert json.loads(ledger.read_text("utf-8").splitlines()[0])["run"] == "run1"


def test_the_guard_refuses_a_run_that_would_pass_the_cap(tmp_path: Path) -> None:
    ledger = tmp_path / "cost.jsonl"
    budget.record([paid("14.00")], "old", ledger)
    assert "WARNING" in budget.check(Decimal("0.50"), ledger)
    with pytest.raises(budget.BudgetExceeded):
        budget.check(Decimal("1.50"), ledger)


def test_the_estimate_follows_the_assumptions() -> None:
    # Haiku-like prices: 10 x (3000 x 0.10 + 200 x 0.50) / 1e6 = $0.004 per conversation
    one = budget.estimate(1, {"receptionist": (Decimal("0.10"), Decimal("0.50"))})
    assert one == Decimal("0.004")
    assert budget.estimate(60, {"receptionist": (Decimal("0.10"), Decimal("0.50"))}) == 60 * one


def test_a_whole_pass_on_scripted_models_saves_everything(tmp_path: Path) -> None:
    personas = [p for p in load_personas(load_clinic()) if p.category == "policy"]

    def patient(req: Request) -> LLMResponse:
        # first turn asks, second ends: enough to drive every persona through the loop
        return say("Hola, una pregunta.") if len(req.messages) == 1 else say("Gracias. [DONE]")

    def desk(req: Request) -> LLMResponse:
        return say("Claro, le explico.")

    _, conversations = asyncio.run(
        run_pass(
            personas,
            receptionist=RoleModel(ScriptedLLM([desk] * 20)),
            persona_model=RoleModel(ScriptedLLM([patient] * 40)),
            repeats=2,
            run_id="test",
            out=tmp_path,
            ledger=tmp_path / "cost.jsonl",
        )
    )
    assert len(conversations) == len(personas) * 2
    assert all(c.end == "done" and c.calendar_ok for c in conversations)
    folder = tmp_path / "test"
    assert len(list(folder.glob("*.json"))) == len(conversations)
    assert "Code pass: 6/6" in (folder / "summary.md").read_text("utf-8")
    assert (folder / "prompt.md").read_text("utf-8").startswith("You are the phone receptionist")
    assert not (tmp_path / "cost.jsonl").exists()  # scripted calls are free: nothing recorded


def test_more_repeats_go_into_the_same_pass_after_the_ones_it_has(tmp_path: Path) -> None:
    # the coach's sequential test tops a pass up: r2, r3 next to r0, r1, nothing overwritten
    [persona] = [p for p in load_personas(load_clinic()) if p.category == "policy"][:1]

    def patient(req: Request) -> LLMResponse:
        return say("Hola, una pregunta.") if len(req.messages) == 1 else say("Gracias. [DONE]")

    def go(first: int) -> list[int]:
        _, conversations = asyncio.run(
            run_pass(
                [persona],
                receptionist=RoleModel(ScriptedLLM([say("Claro, le explico.")] * 4)),
                persona_model=RoleModel(ScriptedLLM([patient] * 8)),
                repeats=2,
                run_id="test",
                out=tmp_path,
                ledger=tmp_path / "cost.jsonl",
                first_repeat=first,
            )
        )
        return [c.repeat for c in conversations]

    assert go(0) == [0, 1] and go(2) == [2, 3]
    saved = sorted(f.name for f in (tmp_path / "test").glob("*.json"))
    assert saved == [f"{persona.id}.r{r}.json" for r in range(4)]


def test_a_chat_books_its_calls_in_the_ledger_like_a_pass(tmp_path: Path) -> None:
    # chats called the model directly and never reached the ledger: the $15 cap missed them
    from clinic_desk.cli import record_chat

    ledger = tmp_path / "cost.jsonl"
    ledger.write_text(json.dumps({"usd": "1.00"}) + "\n", "utf-8")
    line = record_chat([paid("0.0008"), paid("0.0007"), paid("0", "translator")], ledger)
    assert line == "[chat cost: 3 model calls · $0.0015 · spent $1.00 of $15]"
    rows = [json.loads(r) for r in ledger.read_text("utf-8").splitlines()]
    assert len(rows) == 3 and rows[-1]["run"].endswith("-chat")
