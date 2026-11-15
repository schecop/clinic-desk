"""The coach with a scripted model and fake passes: the edit checks, the keep rule, the loop,
the change log, and what the coach is (not) shown."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

from clinic_desk.coach.loop import (
    Pass,
    Split,
    coach_loop,
    decide_visible,
    history_for_coach,
)
from clinic_desk.coach.propose import (
    CoachError,
    Edit,
    EditRejected,
    apply_edit,
    check_failures,
    coach_system,
    failures_brief,
    name_words,
    propose,
)
from clinic_desk.llm.base import UserMessage
from clinic_desk.llm.scripted import ScriptedLLM, call
from tests.test_gate import IDS, first, run

PROMPT = "You are the receptionist.\n\n## Tone\nWarm and patient.\n"
NAMES = name_words(["Aina Serra", "Joan Puig"])
HELD_OUT = [f"h{i:02}" for i in range(10)]
CATS = {f"p{i:02}": ("book", "cancel")[i % 2] for i in range(10)}  # p00 book, p01 cancel, ...


def edit(**over: Any) -> dict[str, Any]:
    base = {
        "target": "tone",
        "pattern": "the call ends without asking if there is anything else (6 conversations)",
        "cause": "the prompt never says how to close",
        "failures": ["p00 r0", "p01 r0", "p02 r0"],
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


def tone_fails() -> Any:
    data, _ = run("v", first(6, "tone"))  # p00, p01, p02, both repeats
    return data


def test_a_rejected_proposal_is_retried_once_with_the_reason() -> None:
    llm = ScriptedLLM([call("propose_edit", **edit(old="Cold.")), call("propose_edit", **edit())])
    got, prompt = asyncio.run(propose(llm, PROMPT, tone_fails(), "", NAMES, CATS))
    assert got.target == "tone" and "anything else" in prompt
    retry = llm.requests[1].messages[0]
    assert isinstance(retry, UserMessage) and "appears 0 times" in retry.content


def test_two_bad_proposals_raise() -> None:
    llm = ScriptedLLM([call("propose_edit", **edit(old="Cold."))] * 2)
    with pytest.raises(CoachError, match="after 2 attempts"):
        asyncio.run(propose(llm, PROMPT, tone_fails(), "", NAMES, CATS))


@pytest.mark.parametrize(
    ("failures", "cats", "message"),
    [
        (["p00 r0", "p01 r0", "p09 r0"], CATS, "p09 r0 did not fail `tone`"),
        (["p00 r0", "p00 r1", "p01 r0"], CATS, "covers 2 patients in 2 categories"),
        (["p00 r0", "p01 r0", "p02 r0"], dict.fromkeys(CATS, "book"), "3 patients in 1 categ"),
    ],
)
def test_a_change_must_fix_a_real_pattern_across_patients(
    failures: list[str], cats: dict[str, str], message: str
) -> None:
    with pytest.raises(EditRejected, match=message):
        check_failures(Edit(**edit(failures=failures)), tone_fails(), cats)
    check_failures(Edit(**edit()), tone_fails(), CATS)  # p00, p01, p02: book and cancel


def test_a_narrow_proposal_is_sent_back_before_anything_is_spent() -> None:
    narrow = edit(failures=["p00 r0", "p00 r1"])
    llm = ScriptedLLM([call("propose_edit", **narrow), call("propose_edit", **edit())])
    got, _ = asyncio.run(propose(llm, PROMPT, tone_fails(), "", NAMES, CATS))
    assert got.failures == ["p00 r0", "p01 r0", "p02 r0"]
    retry = llm.requests[1].messages[0]
    assert isinstance(retry, UserMessage) and "covers 1 patients" in retry.content


def test_the_coach_reads_the_rubric_and_the_visible_failures() -> None:
    system = coach_system()
    assert "no_medical_advice" in system and "ca: li + ajudar/registrar (language)" in system
    assert "{" not in system  # every placeholder filled
    data, _ = run("v", first(2, "tone"))
    brief = failures_brief(data, CATS)
    assert "- tone: 90% (18/20)" in brief and "## tone" in brief
    assert "2 conversations fail · 1 patients · categories: book" in brief
    assert "- p00 r0 [book]: turn 2: quote tone" in brief


def test_lint_failures_reach_the_coach_counted_by_pattern() -> None:
    data, _ = run("v", first(6, "language"))
    rule = "es: con gusto (Latin American)"
    for f in data.failures[0].items[:4]:  # 4 of the 6 are the lint's
        f.evidence, f.reason = f"turn 1: {rule} ('con gusto')", ""
    brief = failures_brief(data, CATS)
    assert f"- `{rule}`: 4 conversations · p00 r0, p00 r1, p01 r0, p01 r1" in brief
    assert "Judge findings (2 of 2 shown):" in brief and "- p02 r0 [book]" in brief


# --- keep rule ------------------------------------------------------------------------------


def p(run_id: str, fails: Any = None, ids: list[str] | None = None, repeats: int = 2) -> Pass:
    data, conversations = run(run_id, fails, ids or IDS, repeats)
    return Pass(run_id, data, conversations)


def test_a_target_rise_within_the_noise_is_rolled_back() -> None:
    d, _ = decide_visible("tone", p("b", first(2, "tone")), p("c"))  # 18/20 -> 20/20: z 1.5
    assert not d.kept and "under 2.2" in d.why and d.promising  # worth more repeats


def test_more_repeats_start_at_one_standard_error() -> None:
    d, _ = decide_visible("tone", p("b", first(1, "tone")), p("c"))  # 19/20 -> 20/20: z 1.01
    assert not d.kept and d.promising
    d, _ = decide_visible("tone", p("b", first(1, "tone")), p("c", first(1, "tone")))  # z 0
    assert not d.kept and not d.promising


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
    measured: dict[str, tuple[str, Split]] = {}  # run id -> what it measured, for `extend`
    repeats_asked: list[tuple[str, int]] = []

    def make(prompt: str, split: Split, label: str, repeats: int) -> Pass:
        ids = IDS if split == "visible" else HELD_OUT
        measured[label] = (prompt, split)
        if prompt == PROMPT:  # the start: tone fails in 8 conversations, on both splits
            return p(label, first(8, "tone"), ids, repeats)
        if overfit in prompt and split == "held_out":  # wins on visible, breaks held-out format
            fails = {(h, r): ("format",) for h in HELD_OUT for r in range(repeats)}
            return p(label, fails, ids, repeats)
        return p(label, None, ids, repeats)

    async def evaluate(prompt: str, split: Split, label: str, repeats: int) -> Pass:
        repeats_asked.append((label, repeats))
        return make(prompt, split, label, repeats)

    async def extend(old: Pass, more: int) -> Pass:
        prompt, split = measured[old.run_id]
        repeats_asked.append((f"{old.run_id}+", more))
        return make(prompt, split, old.run_id, max(c.repeat for c in old.conversations) + 1 + more)

    seen: list[str] = []
    script = [edit(new=overfit), edit(new=good), None]

    async def fake_propose(prompt: str, data: Any, history: str) -> tuple[Edit, str]:
        seen.append(history + failures_brief(data, CATS))
        step = script.pop(0)
        if step is None:
            raise CoachError("no valid proposal after 2 attempts: `old` appears 0 times")
        e = Edit(**step)
        return e, apply_edit(prompt, e, NAMES)

    best, entries = asyncio.run(
        coach_loop(
            PROMPT,
            propose=fake_propose,
            evaluate=evaluate,
            extend=extend,
            iterations=3,
            out=tmp_path,
        )
    )
    # start 4 repeats; each candidate 2, promising (32/40 -> 20/20: z 2.1), so 2 more (z 3.0);
    # held-out 4 repeats, start and candidate
    assert repeats_asked == [
        ("coach-v0", 4),
        ("coach-v1", 2),
        ("coach-v1+", 2),
        ("coach-h0", 4),
        ("coach-h1", 4),
        ("coach-v2", 2),
        ("coach-v2+", 2),
        ("coach-h2", 4),
    ]
    assert [e.kept for e in entries] == [False, True, False]
    assert [e.extended for e in entries] == [True, True, False]
    assert entries[0].why.startswith("target tone rose z +3.0; held-out regression: format")
    assert entries[1].why == "target tone rose z +3.0; held-out: no regression"
    assert entries[1].target_before == "80% (32/40)" and entries[1].target_after == "100% (40/40)"
    assert entries[1].pattern.startswith("the call ends without asking")
    assert entries[2].target == "" and "appears 0 times" in entries[2].why
    assert best == PROMPT.replace("Warm and patient.", good)
    assert (tmp_path / "best.md").read_text("utf-8") == best
    assert (tmp_path / "start.md").read_text("utf-8") == PROMPT
    assert len((tmp_path / "changelog.jsonl").read_text("utf-8").splitlines()) == 3
    md = (tmp_path / "changelog.md").read_text("utf-8")
    assert "| 2 | tone | 80% (32/40) → 100% (40/40) |" in md and "✅ kept (extended)" in md
    assert "- **Cause (coach):** the prompt never says how to close" in md
    assert "+Warm and patient. Close by asking if there is anything else." in md
    # what the coach read: the rollback, but no held-out persona, run, criterion or score
    assert "patients you do not see" in seen[1] and "pattern: the call ends" in seen[1]
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


def test_card_failures_count_by_conversation_and_patient() -> None:
    data, _ = run("v", first(6, "card_1"))  # the report groups card lines as "card"
    brief = failures_brief(data, CATS)
    assert (
        "## card" in brief
        and "6 conversations fail · 3 patients · categories: book, cancel" in brief
    )
    check_failures(Edit(**edit(target="card")), data, CATS)  # "p00 r0" matches "p00 r0 · card_1…"
