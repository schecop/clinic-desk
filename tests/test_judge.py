"""The judge and its calibration, with a scripted judge model: prompt contents, parsing, retry,
the label template round trip, and the agreement table."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from clinic_desk.clinic import load_clinic
from clinic_desk.llm.scripted import ScriptedLLM, call, say
from clinic_desk.simulator.calibration import agreement, label_template, load_labels, pick
from clinic_desk.simulator.judge import RUBRIC, criteria_for, judge, judge_prompt
from clinic_desk.simulator.persona import load_personas
from tests.test_traces import carmen

CLINIC = load_clinic()
PERSONAS = {p.id: p for p in load_personas(CLINIC)}
CARMEN = PERSONAS["carmen-move-afternoon"]


def entries(fail: str | None = None, na: tuple[str, ...] = ()) -> list[dict[str, Any]]:
    return [
        {
            "id": cid,
            "evidence": "turn 1: …",
            "reason": "r",
            "applies": cid not in na,
            "pass": cid != fail,
        }
        for cid in criteria_for(CARMEN)
    ]


def test_the_prompt_has_the_card_lines_the_rubric_the_facts_and_the_tool_log() -> None:
    prompt = judge_prompt(carmen(), CARMEN, CLINIC)
    assert "`card_1`: Si pregunta por el coste del cambio" in prompt
    assert all(f"`{k}`" in prompt for k in RUBRIC)
    assert "## cancellation" in prompt and "opening_hours" in prompt
    assert "    tools: find_slots(" in prompt and "    write: moved A003" in prompt
    assert "{" + "conversation}" not in prompt


def test_a_verdict_comes_from_the_tool_call() -> None:
    llm = ScriptedLLM([call("record_verdict", criteria=entries(fail="tone", na=("card_1",)))])
    v = asyncio.run(judge(llm, carmen(), CARMEN, CLINIC, "run/carmen.r0"))
    assert v.error is None and not v.passed
    labels = {c.id: c.label for c in v.criteria}
    assert labels["tone"] == "no" and labels["card_1"] == "na" and labels["facts_right"] == "yes"
    assert [c.id for c in v.criteria] == list(criteria_for(CARMEN))


def test_a_verdict_missing_a_criterion_is_retried_then_recorded_as_an_error() -> None:
    short = entries()[:-1]
    llm = ScriptedLLM([call("record_verdict", criteria=short), say("{}")])
    v = asyncio.run(judge(llm, carmen(), CARMEN, CLINIC, "run/carmen.r0"))
    assert v.error and not v.passed
    assert "rejected" in llm.requests[1].messages[0].content  # type: ignore[union-attr]

    llm = ScriptedLLM([say("no"), call("record_verdict", criteria=entries())])
    assert asyncio.run(judge(llm, carmen(), CARMEN, CLINIC, "x")).passed


def test_json_in_the_text_is_accepted_when_the_model_skips_the_tool() -> None:
    import json

    text = "Here: " + json.dumps({"criteria": entries()})
    v = asyncio.run(judge(ScriptedLLM([say(text)]), carmen(), CARMEN, CLINIC, "x"))
    assert v.passed


def test_json_mode_sends_no_tool_and_lists_every_id() -> None:
    import json

    llm = ScriptedLLM([say(json.dumps({"criteria": entries(fail="format")}))])
    v = asyncio.run(judge(llm, carmen(), CARMEN, CLINIC, "x", mode="json"))
    assert not v.passed and v.by_id()["format"].label == "no"
    request = llm.requests[0]
    assert not request.tools
    assert all(cid in request.messages[0].content for cid in criteria_for(CARMEN))  # type: ignore[union-attr]


def test_labels_round_trip_and_agreement(tmp_path: Path) -> None:
    c = carmen()
    chosen = pick([c], PERSONAS, n=20)
    path = tmp_path / "labels.yaml"
    template = label_template("run", chosen, PERSONAS)
    assert "card_1:   # Si pregunta por el coste" in template
    path.write_text(template, "utf-8")
    assert load_labels(path) == []  # nothing filled in yet: not used

    filled = template
    for cid in criteria_for(CARMEN):
        value = "na" if cid in ("card_1", "no_medical_advice") else "yes"
        filled = filled.replace(f"    {cid}:", f"    {cid}: {value}", 1)
    filled = filled.replace("    tone: yes", "    tone: no")
    path.write_text(filled, "utf-8")
    [item] = load_labels(path)
    assert item.labels["tone"] == "no" and item.labels["card_1"] == "na"

    llm = ScriptedLLM(
        [call("record_verdict", criteria=entries(na=("card_1", "no_medical_advice")))]
    )
    v = asyncio.run(judge(llm, c, CARMEN, CLINIC, item.conversation))
    table = agreement([item], {item.conversation: v})
    assert "| tone | 0/1 (0%) |" in table
    assert "| facts_right | 1/1 (100%) |" in table
    assert "| **conversation pass/fail** | 0/1 (0%) |" in table  # human fails it, judge passes
    assert "| run/carmen-move-afternoon.r0 | tone | no | yes |" in table


def test_pick_covers_every_persona_before_repeats() -> None:
    a, b = carmen(), carmen().model_copy(update={"repeat": 1})
    other = carmen().model_copy(update={"persona_id": "minors-alone"})
    assert [(x.persona_id, x.repeat) for x in pick([b, a, other], PERSONAS, n=2)] == [
        ("carmen-move-afternoon", 0),
        ("minors-alone", 0),
    ]


def test_a_pass_is_judged_saved_and_summarised(tmp_path: Path) -> None:
    from clinic_desk.simulator.evaluate import judge_pass, load_verdicts
    from clinic_desk.simulator.simulate import summary

    c = carmen()
    llm = ScriptedLLM([call("record_verdict", criteria=entries(fail="tone"))])
    merged, alone = asyncio.run(
        judge_pass(
            [c], llm, run_id="run", folder=tmp_path, role="judge", personas=PERSONAS, clinic=CLINIC
        )
    )
    name = "run/carmen-move-afternoon.r0"
    assert not merged[name].passed and not alone[name].passed
    assert load_verdicts("run", "judge", tmp_path) == merged
    text = summary("run", [c], None, merged)
    assert (
        "- Judge (scripted, with the lint): 0/1 pass" in text and "failing criteria: tone 1" in text
    )
    assert "| ❌ tone |" in text and "Full pass" not in text
    assert "Criteria passed: 6/7 (86%)" in text


def test_the_report_scores_splits_criteria_personas_and_failures() -> None:
    from clinic_desk.simulator.report import analyse, to_html, to_markdown

    r0, r1 = carmen(), carmen().model_copy(update={"repeat": 1})
    script0 = [call("record_verdict", criteria=entries(fail="tone"))]
    script1 = [call("record_verdict", criteria=entries(fail="card_1"))]
    v0 = asyncio.run(judge(ScriptedLLM(script0), r0, CARMEN, CLINIC, "run/" + CARMEN.id + ".r0"))
    v1 = asyncio.run(judge(ScriptedLLM(script1), r1, CARMEN, CLINIC, "run/" + CARMEN.id + ".r1"))
    data = analyse(
        "run", [r0, r1], {v.conversation: v for v in (v0, v1)}, {CARMEN.id: "visible"}, PERSONAS
    )
    md = to_markdown(data)
    assert "| visible | 2 | **86% (12/14)** | 2/2 |" in md and "full pass" not in md.lower()
    assert "| tone | 50% (1/2) | 1 | 0 |" in md
    assert f"| {CARMEN.id} | visible | move | 86% · 86% | 86% | 0% | 2/2 | card 1, tone 1 |" in md
    # a card failure carries the card's own line, so it reads without opening the card
    assert f"- {CARMEN.id} r1 · card_1: Si pregunta por el coste" in md
    page = to_html(data)
    assert page.startswith("<!doctype html>") and "prefers-color-scheme:dark" in page
    assert "card_1: Si pregunta por el coste" in page and "http" not in page.split("<script>")[1]
    assert f'data-persona="{CARMEN.id}"' in page and 'data-crit="tone"' in page
