"""The coach's proposal: what it reads, the one edit it returns, and the checks on that edit.

The coach sees the current prompt, the failures of the **visible** pass (grouped by criterion,
with the judge's or the lint's evidence) and the history of earlier attempts. It never sees a
held-out conversation, persona id or score: the history says only that a held-out check failed.

Its answer is one exact replacement (`old` → `new`), checked by code before anything is spent:
`old` must appear exactly once, the change must not grow the prompt past `MAX_GROWTH`, and `new`
must not name a patient (a rule written for one transcript is overfitting). A rejected answer is
sent back once with the reason.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from functools import cache
from pathlib import Path

from pydantic import BaseModel, ValidationError

from clinic_desk.llm.base import LLMClient, ToolSpec, UserMessage
from clinic_desk.simulator.judge import RUBRIC
from clinic_desk.simulator.lint import RULES
from clinic_desk.simulator.report import ReportData

PROMPT_PATH = Path("prompts/coach.md")
MAX_GROWTH = 1200  # characters one change may add
PER_GROUP = 6  # failures shown per criterion
MAX_TOKENS = 16000  # thinking counts toward it
TARGETS = [*RUBRIC, "card", "code checks"]


class Edit(BaseModel):
    target: str
    failures: list[str]
    reason: str
    risk: str
    old: str
    new: str


class EditRejected(ValueError):
    pass


class CoachError(RuntimeError):
    pass


EDIT_TOOL = ToolSpec(
    name="propose_edit",
    description="Propose exactly one change to the receptionist prompt.",
    input_schema={
        "type": "object",
        "properties": {
            "target": {"type": "string", "enum": TARGETS},
            "failures": {"type": "array", "items": {"type": "string"}},
            "reason": {"type": "string"},
            "risk": {"type": "string"},
            "old": {"type": "string"},
            "new": {"type": "string"},
        },
        "required": ["target", "failures", "reason", "risk", "old", "new"],
    },
)


def name_words(names: Sequence[str]) -> set[str]:
    """Words of the patients' names worth blocking (4+ letters: 'Joan', 'Serra', not 'de')."""
    return {w.lower() for n in names for w in re.findall(r"[\wÀ-ÿ·]+", n) if len(w) >= 4}


def apply_edit(prompt: str, edit: Edit, names: set[str]) -> str:
    if edit.target not in TARGETS:
        raise EditRejected(f"target {edit.target!r} is not one of {TARGETS}")
    if not edit.old.strip():
        raise EditRejected("`old` is empty: copy the passage to replace from the prompt")
    n = prompt.count(edit.old)
    if n != 1:
        raise EditRejected(
            f"`old` appears {n} times in the prompt; it must appear exactly once, character"
            " for character"
        )
    if edit.new == edit.old:
        raise EditRejected("`new` is the same as `old`")
    grown = len(edit.new) - len(edit.old)
    if grown > MAX_GROWTH:
        raise EditRejected(f"the change adds {grown} characters; keep it under {MAX_GROWTH}")
    added = set(re.findall(r"[\wÀ-ÿ·]+", edit.new.lower())) - set(
        re.findall(r"[\wÀ-ÿ·]+", edit.old.lower())
    )
    named = sorted(added & names)
    if named:
        raise EditRejected(f"`new` names a patient ({', '.join(named)}): write a general rule")
    return prompt.replace(edit.old, edit.new, 1)


def failures_brief(d: ReportData, per_group: int = PER_GROUP) -> str:
    """The visible pass as the coach reads it: scores per criterion, then failures with evidence."""
    lines = ["Criteria (pass rate over the visible pass):"]
    lines += [f"- {r.id}: {r.score}" for r in d.criteria]
    for g in d.failures:
        if g.id == "judge errors":
            continue
        lines += ["", f"## {g.id} ({len(g.items)} failures){': ' + g.title if g.title else ''}"]
        for f in g.items[:per_group]:
            why = f" — {f.reason}" if f.reason else ""
            lines.append(f"- {f.who}: {f.evidence[:400]}{why[:300]}")
        if len(g.items) > per_group:
            lines.append(f"- … {len(g.items) - per_group} more")
    return "\n".join(lines)


@cache
def _template() -> str:
    return PROMPT_PATH.read_text(encoding="utf-8")


def coach_system() -> str:
    rubric = "\n".join(f"- `{k}`: {v}" for k, v in RUBRIC.items())
    rubric += (
        "\n- `card`: the scripted patient's own success lines (e.g. what the call must achieve)"
    )
    rubric += "\n- `code checks`: task done in the calendar, no false claim, right language"
    lint = "; ".join(sorted({f"{r.id} ({r.criterion})" for r in RULES}))
    return (
        _template()
        .replace("{rubric}", rubric)
        .replace("{lint}", lint)
        .replace("{targets}", ", ".join(TARGETS))
    )


def coach_message(prompt: str, brief: str, history: str) -> str:
    return (
        f"# Current receptionist prompt\n\n<prompt>\n{prompt}\n</prompt>\n\n"
        f"# Earlier attempts\n\n{history or 'None yet.'}\n\n"
        f"# Failures in the latest visible pass\n\n{brief}\n\n"
        "Propose one change now: call propose_edit."
    )


async def propose(
    llm: LLMClient,
    prompt: str,
    brief: str,
    history: str,
    names: set[str],
    attempts: int = 2,
) -> tuple[Edit, str]:
    """One edit and the prompt it makes; a rejected answer is retried once with the reason."""
    system, ask = coach_system(), coach_message(prompt, brief, history)
    problem = ""
    for _ in range(attempts):
        message = ask + (f"\n\nYour previous proposal was rejected: {problem}" if problem else "")
        response = await llm.complete(
            system=system, messages=[UserMessage(message)], tools=[EDIT_TOOL], max_tokens=MAX_TOKENS
        )
        call = next((t for t in response.tool_calls if t.name == "propose_edit"), None)
        if call is None:
            problem = "no propose_edit call"
            continue
        try:
            edit = Edit.model_validate(call.arguments)
            return edit, apply_edit(prompt, edit, names)
        except (ValidationError, EditRejected) as err:
            problem = str(err)[:500]
    raise CoachError(f"no valid proposal after {attempts} attempts: {problem}")
