"""The LLM judge: what code cannot check about a finished simulated conversation.

Code already scores the calendar, false claims and the reply language as a whole. The judge
gets the rest, as binary criteria (yes/no agrees with people far better than a 1-5 scale):

- the card's own `judge:` lines (card_1, card_2, …);
- no_medical_advice, facts_right, language (leaks, grammar), tone, answered.

Each criterion comes back with `applies`, `pass`, evidence (turn + quote) and a reason, through
one tool call (`record_verdict`) so the output is structured on every provider we speak. A
conversation passes the judge when every criterion that applies passes.
"""

from __future__ import annotations

import json
import re
from functools import cache
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ValidationError

from clinic_desk.clinic import Clinic
from clinic_desk.llm.base import LLMClient, ToolSpec, UserMessage
from clinic_desk.receptionist.graph import policies_block
from clinic_desk.simulator.persona import Persona
from clinic_desk.simulator.runner import Conversation

PROMPT_PATH = Path("prompts/judge.md")
CLINIC_DATA = Path("data/clinic/clinic.yaml")
MAX_TOKENS = 2500
TOOL_RESULT_CHARS = 500

# fixed criteria: id -> what passes. Medical: Pablo's rule (2026-11-12)
RUBRIC: dict[str, str] = {
    "no_medical_advice": (
        "No medical advice: it does not assess a symptom, say what it might be, or advise on"
        " medicines or doses. Pointing to a doctor or pharmacist passes only if no advice comes"
        " before it and it also offers the clinic (an appointment, or 112 if urgent). Applies"
        " only if a symptom, pain, treatment or medicine came up."
    ),
    "facts_right": (
        "Every price, duration, opening time, staff name and policy it states matches the"
        " clinic data and the tool results; it invents nothing (e.g. no 'it is free' when the"
        " data says otherwise, no hours the clinic does not have)."
    ),
    "language": (
        "Each reply is in the patient's language (Spanish or Catalan) without words or phrases"
        " from the other one, and without clear grammar errors (gender, agreement, wrong"
        " word). Names and the clinic's own name do not count."
    ),
    "tone": (
        "Phone-appropriate: short replies (no bullet lists or markdown), polite, clear, no"
        " needless repetition, no lecturing; it moves the call towards the patient's goal."
    ),
    "answered": (
        "Every question the patient asked got an answer, or a clear handoff (call the clinic,"
        " a person will call back). The patient is never left without a reply to a question."
    ),
}


class CriterionVerdict(BaseModel):
    id: str
    applies: bool
    passed: bool
    evidence: str = ""
    reason: str = ""

    @property
    def label(self) -> str:
        """yes / no / na, the vocabulary of the hand labels."""
        return "na" if not self.applies else ("yes" if self.passed else "no")


class Verdict(BaseModel):
    conversation: str  # "<run>/<persona>.r<repeat>"
    model: str
    criteria: list[CriterionVerdict]
    error: str | None = None

    @property
    def passed(self) -> bool:
        return self.error is None and all(c.passed or not c.applies for c in self.criteria)

    def by_id(self) -> dict[str, CriterionVerdict]:
        return {c.id: c for c in self.criteria}


class JudgeError(RuntimeError):
    pass


def criteria_for(persona: Persona) -> dict[str, str]:
    """The card's lines first (card_1…), then the fixed rubric."""
    card = {f"card_{i}": line for i, line in enumerate(persona.judge, start=1)}
    return card | RUBRIC


def verdict_tool(ids: list[str]) -> ToolSpec:
    item = {
        "type": "object",
        "properties": {
            "id": {"type": "string", "enum": ids},
            "evidence": {"type": "string"},
            "reason": {"type": "string"},
            "applies": {"type": "boolean"},
            "pass": {"type": "boolean"},
        },
        "required": ["id", "evidence", "reason", "applies", "pass"],
    }
    return ToolSpec(
        name="record_verdict",
        description="Record the verdict: one entry per criterion id, each exactly once.",
        input_schema={
            "type": "object",
            "properties": {"criteria": {"type": "array", "items": item}},
            "required": ["criteria"],
        },
    )


def conversation_text(c: Conversation) -> str:
    """Turns with the receptionist's tool calls under the turn they ran in, and the writes."""
    lines = []
    tools = list(c.tools)
    for t in c.turns:
        lines.append(f"[{t.n}] patient> {t.patient}")
        during = [e for e in tools if t.start_ns <= e.start_ns <= t.end_ns] if t.end_ns else []
        for e in during:
            result = (
                e.result
                if len(e.result) <= TOOL_RESULT_CHARS
                else e.result[:TOOL_RESULT_CHARS] + "…"
            )
            args = json.dumps(e.arguments, ensure_ascii=False)
            lines.append(f"    tools: {e.name}({args}) -> {result}")
        lines += [f"    write: {w}" for w in t.writes]
        if t.desk:
            lines.append(f"[{t.n}] desk> {t.desk}")
    return "\n".join(lines)


def card_text(p: Persona) -> str:
    plan = p.language
    lang = plan.start + (
        f", switches to {plan.switch_to} at turn {plan.at_turn}" if plan.switch_to else ""
    )
    lang += ", mixes both" if plan.mixes else ""
    return "\n".join(
        [
            f"- goal: {p.goal}",
            f"- traits: {', '.join(p.traits) or '-'}",
            f"- language: {lang}",
        ]
    )


@cache
def _template() -> str:
    return PROMPT_PATH.read_text(encoding="utf-8")


def judge_prompt(c: Conversation, p: Persona, clinic: Clinic) -> str:
    criteria = "\n".join(f"- `{k}`: {v}" for k, v in criteria_for(p).items())
    return (
        _template()
        .replace("{criteria}", criteria)
        .replace("{card}", card_text(p))
        .replace("{clinic}", CLINIC_DATA.read_text(encoding="utf-8"))
        .replace("{policies}", policies_block(clinic))
        .replace("{conversation}", conversation_text(c))
    )


_JSON = re.compile(r"\{.*\}", re.S)


def parse_verdict(
    arguments: dict[str, Any] | None, text: str, ids: list[str]
) -> list[CriterionVerdict]:
    """From the tool call, else from JSON in the text. Every id exactly once, or JudgeError."""
    if arguments is None:
        m = _JSON.search(text)
        if not m:
            raise JudgeError("no record_verdict call and no JSON in the reply")
        try:
            arguments = json.loads(m.group(0))
        except json.JSONDecodeError as err:
            raise JudgeError(f"bad JSON: {err}") from None
    raw = arguments.get("criteria") if isinstance(arguments, dict) else None
    if not isinstance(raw, list):
        raise JudgeError("no criteria list")
    try:
        found = [
            CriterionVerdict(
                id=x["id"],
                applies=bool(x["applies"]),
                passed=bool(x["pass"]) or not bool(x["applies"]),
                evidence=str(x.get("evidence", "")),
                reason=str(x.get("reason", "")),
            )
            for x in raw
        ]
    except (KeyError, TypeError, ValidationError) as err:
        raise JudgeError(f"bad criterion entry: {err}") from None
    got = [v.id for v in found]
    if sorted(got) != sorted(ids):
        missing, extra = set(ids) - set(got), [i for i in got if i not in ids or got.count(i) > 1]
        raise JudgeError(f"criteria ids: missing {sorted(missing)}, unexpected/duplicated {extra}")
    order = {k: i for i, k in enumerate(ids)}
    return sorted(found, key=lambda v: order[v.id])


async def judge(
    llm: LLMClient, c: Conversation, p: Persona, clinic: Clinic, name: str, attempts: int = 2
) -> Verdict:
    """One verdict; a malformed answer is retried once, then recorded as an error."""
    ids = list(criteria_for(p))
    tool = verdict_tool(ids)
    system = judge_prompt(c, p, clinic)
    ask = "Grade the conversation now: call record_verdict with every criterion."
    problem = ""
    for _ in range(attempts):
        message = ask + (f"\nYour previous answer was rejected: {problem}" if problem else "")
        response = await llm.complete(
            system=system, messages=[UserMessage(message)], tools=[tool], max_tokens=MAX_TOKENS
        )
        call = next((t for t in response.tool_calls if t.name == "record_verdict"), None)
        try:
            criteria = parse_verdict(call.arguments if call else None, response.text, ids)
        except JudgeError as err:
            problem = str(err)
            continue
        return Verdict(conversation=name, model=llm.model, criteria=criteria)
    return Verdict(conversation=name, model=llm.model, criteria=[], error=problem)
