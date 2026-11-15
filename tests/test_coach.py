"""The coach with a scripted model and fake passes: the edit checks, the keep rule, the loop,
the change log, and what the coach is (not) shown."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

from clinic_desk.coach.loop import Pass, Split, coach_loop, decide_visible, history_for_coach
from clinic_desk.coach.propose import (
    CoachError,
    Edit,
    EditRejected,
    apply_edit,
    coach_system,
    failures_brief,
    name_words,
    propose,
)
from clinic_desk.llm.base import UserMessage
from clinic_desk.llm.scripted import ScriptedLLM, call
from tests.test_gate import first, run

PROMPT = "You are the receptionist.\n\n## Tone\nWarm and patient.\n"
NAMES = name_words(["Aina Serra", "Joan Puig"])
HELD_OUT = [f"h{i:02}" for i in range(10)]


def edit(**over: Any) -> dict[str, Any]:
    base = {
        "target": "tone",
        "failures": ["p00 r0", "p01 r0"],
        "reason": "closes without asking if there is anything else",
        "risk": "longer replies",
        "old": "Warm and patient.",
        "new": "Warm and patient. Close by asking if there is anything else.",
    }
    return base | over


def test_an_edit_replaces_one_exact_passage() -> None:
    out = apply_edit(PROMPT, Edit(**edit()), NAMES)
    assert out == PROMPT.replace("Warm and patient.", edit()["new"])


@pytest.mark.parametrize(
    ("over", "message"),
    [
        ({"old": "Cold."}, "appears 0 times"),
        ({"old": "e"}, "appears"),
        ({"old": " "}, "`old` is empty"),
        ({"new": "Warm and patient."}, "same as `old`"),
        ({"new": "x" * 1300}, "adds"),
        ({"new": "Warm and patient. If Aina calls, book her first."}, "names a patient (aina)"),
        ({"target": "speed"}, "not one of"),
    ],
)
def test_a_bad_edit_is_rejected_with_a_reason(over: dict[str, Any], message: str) -> None:
    with pytest.raises(EditRejected, match=message.replace("(", r"\(").replace(")", r"\)")):
        apply_edit(PROMPT, Edit(**edit(**over)), NAMES)


def test_a_rejected_proposal_is_retried_once_with_the_reason() -> None:
    llm = ScriptedLLM([call("propose_edit", **edit(old="Cold.")), call("propose_edit", **edit())])
    got, prompt = asyncio.run(propose(llm, PROMPT, "brief", "", NAMES))
    assert got.target == "tone" and "anything else" in prompt
    retry = llm.requests[1].messages[0]
    assert isinstance(retry, UserMessage) and "appears 0 times" in retry.content


def test_two_bad_proposals_raise() -> None:
    llm = ScriptedLLM([call("propose_edit", **edit(old="Cold."))] * 2)
    with pytest.raises(CoachError, match="after 2 attempts"):
        asyncio.run(propose(llm, PROMPT, "brief", "", NAMES))


def test_the_coach_reads_the_rubric_and_the_visible_failures() -> None:
    system = coach_system()
    assert "no_medical_advice" in system and "ca: li + ajudar/registrar (language)" in system
    data, _ = run("v", first(2, "tone"))
    brief = failures_brief(data)
    assert "- tone: 90% (18/20)" in brief and "## tone (2 failures)" in brief
    assert "p00 r0: turn 2: quote tone" in brief


# --- keep rule ------------------------------------------------------------------------------


def p(run_id: str, fails: Any = None, ids: list[str] | None = None) -> Pass:
    data, conversations = run(run_id, fails, ids) if ids else run(run_id, fails)
    return Pass(run_id, data, conversations)


def test_a_target_rise_within_the_noise_is_rolled_back() -> None:
    d, _ = decide_visible("tone", p("b", first(2, "tone")), p("c"))  # 18/20 -> 20/20: z 1.5
    assert not d.kept and "under 2" in d.why


def test_a_target_rise_beyond_the_noise_is_kept() -> None:
    d, _ = decide_visible("tone", p("b", first(8, "tone")), p("c"))  # 12/20 -> 20/20: z 3.3
    assert d.kept and d.target_z > 2


def test_a_rise_that_breaks_another_criterion_is_rolled_back() -> None:
    d, _ = decide_visible("tone", p("b", first(8, "tone")), p("c", first(8, "format")))
    assert not d.kept and d.why == "visible regression: format"  # the total did not move


# --- the loop -------------------------------------------------------------------------------


def test_the_loop_keeps_rolls_back_logs_and_never_shows_held_out(tmp_path: Path) -> None:
    good = "Warm and patient. Close by asking if there is anything else."
    overfit = "Warm and patient. Close every call with a full summary in a list."

    async def evaluate(prompt: str, split: Split, label: str) -> Pass:
        ids = None if split == "visible" else HELD_OUT
        if prompt == PROMPT:  # the start: tone fails in 8 of 20, on both splits
            return p(label, first(8, "tone"), ids)
        if overfit in prompt and split == "held_out":  # wins on visible, breaks held-out format
            return p(label, {(h, r): ("format",) for h in HELD_OUT for r in (0, 1)}, ids)
        return p(label, None, ids)

    seen: list[str] = []
    script = [edit(new=overfit), edit(new=good), None]

    async def fake_propose(prompt: str, data: Any, history: str) -> tuple[Edit, str]:
        seen.append(history + failures_brief(data))
        step = script.pop(0)
        if step is None:
            raise CoachError("no valid proposal after 2 attempts: `old` appears 0 times")
        e = Edit(**step)
        return e, apply_edit(prompt, e, NAMES)

    best, entries = asyncio.run(
        coach_loop(PROMPT, propose=fake_propose, evaluate=evaluate, iterations=3, out=tmp_path)
    )
    assert [e.kept for e in entries] == [False, True, False]
    assert entries[0].why.startswith("target tone rose z +3.2; held-out regression: format")
    assert entries[1].why == "target tone rose z +3.2; held-out: no regression"
    assert entries[1].target_before == "60% (12/20)" and entries[1].target_after == "100% (20/20)"
    assert entries[2].target == "" and "appears 0 times" in entries[2].why
    assert best == PROMPT.replace("Warm and patient.", good)
    assert (tmp_path / "best.md").read_text("utf-8") == best
    assert (tmp_path / "start.md").read_text("utf-8") == PROMPT
    assert len((tmp_path / "changelog.jsonl").read_text("utf-8").splitlines()) == 3
    md = (tmp_path / "changelog.md").read_text("utf-8")
    assert "| 2 | tone | 60% (12/20) → 100% (20/20) |" in md and "✅ kept" in md
    assert "+Warm and patient. Close by asking if there is anything else." in md
    # what the coach read: the rollback, but no held-out persona, run, criterion or score
    assert "patients you do not see" in seen[1]
    for text in seen:
        assert not any(h in text for h in HELD_OUT) and "coach-h" not in text
        assert "held-out regression" not in text


def test_a_held_out_regression_rolls_back_and_the_coach_hears_only_that() -> None:
    from clinic_desk.coach.loop import Entry

    e = Entry(
        iteration=1,
        target="tone",
        reason="r",
        diff="-a\n+b\n",
        target_before="60%",
        target_after="100%",
        held_out_before="80% (8/10)",
        held_out_after="50% (5/10)",
        held_out_run="coach-h1",
        kept=False,
        why="target tone rose z +3.3; held-out regression: format, all criteria",
    )
    text = history_for_coach([e])
    assert "ROLLED BACK" in text and "patients you do not see" in text
    assert "format" not in text and "50%" not in text and "coach-h1" not in text
