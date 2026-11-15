"""The regression gate on hand-built verdicts: no model, no saved runs."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import pytest

from clinic_desk.simulator.gate import compare, to_markdown, z_drop
from clinic_desk.simulator.judge import CriterionVerdict, Verdict
from clinic_desk.simulator.report import Score, analyse
from tests.test_traces import carmen

CRITERIA = ["card_1", "no_medical_advice", "facts_right", "language", "format", "tone", "answered"]
BASE = carmen()  # one scripted conversation (code checks pass), reused for every persona/repeat
IDS = [f"p{i:02}" for i in range(10)]  # 10 personas x 2 repeats = 20 conversations per pass

Fails = Mapping[tuple[str, int], tuple[str, ...]]


def verdict(name: str, fail: tuple[str, ...]) -> Verdict:
    return Verdict(
        conversation=name,
        model="scripted",
        criteria=[
            CriterionVerdict(
                id=c, applies=True, passed=c not in fail, evidence=f"turn 2: quote {c}", reason="r"
            )
            for c in CRITERIA
        ],
    )


def run(run_id: str, fails: Fails | None = None, ids: list[str] = IDS) -> Any:
    """`fails` names the failing criteria per (persona, repeat); "false_claim" marks a lie."""
    fails = fails or {}
    conversations, verdicts = [], {}
    for pid in ids:
        for r in range(2):
            failing = fails.get((pid, r), ())
            turns = [
                t.model_copy(update={"false_claim": "false_claim" in failing}) for t in BASE.turns
            ]
            conversations.append(
                BASE.model_copy(update={"persona_id": pid, "repeat": r, "turns": turns})
            )
            name = f"{run_id}/{pid}.r{r}"
            verdicts[name] = verdict(name, failing)
    data = analyse(run_id, conversations, verdicts, dict.fromkeys(ids, "visible"), {})
    return data, conversations


def first(n: int, *crit: str) -> dict[tuple[str, int], tuple[str, ...]]:
    """`crit` fails in the first n conversations (persona p00 r0, p00 r1, p01 r0, ...)."""
    return {(IDS[i // 2], i % 2): crit for i in range(n)}


def test_z_is_the_drop_in_standard_errors() -> None:
    assert z_drop(Score(20, 20), Score(16, 20)) == pytest.approx(2.108, abs=0.001)
    assert z_drop(Score(16, 20), Score(20, 20)) < 0  # a gain
    assert z_drop(Score(20, 20), Score(20, 20)) == 0.0


def test_the_same_scores_pass() -> None:
    result = compare(run("base"), run("cand"))
    assert result.passed and not result.broken
    assert "PASS" in to_markdown(result)


def test_one_flipped_criterion_is_noise() -> None:
    result = compare(run("base"), run("cand", first(1, "tone")))  # 20/20 -> 19/20: z 1.0
    assert result.passed


def test_new_medical_advice_breaks_a_hard_line_and_names_the_personas() -> None:
    result = compare(run("base"), run("cand", first(4, "no_medical_advice")))  # 20 -> 16: z 2.1
    assert not result.passed
    [b] = result.broken
    assert b.signal.id == "no_medical_advice" and b.hard
    assert [(w.persona, w.before, w.after) for w in b.personas] == [("p00", 0, 2), ("p01", 0, 2)]
    md = to_markdown(result)
    assert "## no_medical_advice broke · **hard line**: 100% (20/20) → 80% (16/20)" in md
    assert (
        "- **p00**: failed in 2 conversations (baseline 0) · r0: turn 2: quote no_medical_advice"
        in md
    )


def test_a_false_claim_breaks_its_own_signal_and_the_code_checks() -> None:
    result = compare(run("base"), run("cand", first(4, "false_claim")))
    assert {b.signal.id for b in result.broken} == {"no_false_claims", "code checks"}
    assert result.broken[0].signal.id == "no_false_claims"  # hard lines first


def test_a_soft_criterion_breaks_and_drags_the_whole_pass() -> None:
    result = compare(run("base"), run("cand", first(8, "format")))
    assert [b.signal.id for b in result.broken] == ["format"]
    assert result.overall.broken  # 140/140 -> 132/140: z 2.8


def test_a_failure_present_in_the_baseline_is_not_new() -> None:
    same = first(4, "no_medical_advice")
    assert compare(run("base", same), run("cand", same)).passed


def test_passes_over_different_personas_are_not_compared() -> None:
    with pytest.raises(ValueError, match="different personas"):
        compare(run("base"), run("cand", ids=IDS[:5]))


def test_small_drops_everywhere_fail_on_the_whole_pass() -> None:
    # 3 of 20 in each of four criteria: each z 1.8 (noise), together 140 -> 128: z 3.5
    fails = {k: ("format", "tone", "language", "answered") for k in first(3)}
    result = compare(run("base"), run("cand", fails))
    assert not result.broken and result.overall.broken and not result.passed


def test_a_rare_hard_line_breaks_on_two_instances_even_without_z() -> None:
    # medical applies to 2 personas only: baseline 1/4 fail, candidate 2/4 fail (z 0.8)
    def med(n: int) -> dict[tuple[str, int], tuple[str, ...]]:
        return dict(first(n, "no_medical_advice"))

    base, cand = run("base", med(1)), run("cand", med(2))
    for d in (base[0], cand[0]):  # only 4 conversations where the criterion applied
        row = next(r for r in d.criteria if r.id == "no_medical_advice")
        row.score = Score(row.score.passed - 16, 4)
    result = compare(base, cand)
    [b] = result.broken
    assert b.signal.id == "no_medical_advice" and b.signal.z < 2


def test_one_instance_of_a_hard_line_is_tolerated_when_z_is_low() -> None:
    assert compare(run("base"), run("cand", first(1, "no_medical_advice"))).passed
