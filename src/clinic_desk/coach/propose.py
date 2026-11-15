"""The coach's proposal: what it reads, the one edit it returns, and the checks on that edit.

The coach sees the current prompt, the failures of the **visible** pass (per criterion: how
many conversations, patients and categories fail, the lint's exact patterns counted, then the
judge's findings with each patient's category) and the history of earlier attempts. It never
sees a held-out conversation, persona id or score: the history says only that a held-out check
failed.

Its answer is one exact replacement (`old` → `new`) with the pattern it fixes and the cause it
assumes, checked by code before anything is spent: `old` must appear exactly once, the change
must not grow the prompt past `MAX_GROWTH`, `new` must not name a patient, and the conversations
it claims to fix must be real failures of its target from at least `MIN_PATIENTS` patients in
`MIN_CATEGORIES` categories (a rule for one transcript is overfitting, and a fix for one or two
conversations cannot be measured). A rejected answer is sent back once with the reason.
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
from clinic_desk.simulator.report import FailureGroup, ReportData

PROMPT_PATH = Path("prompts/coach.md")
MAX_GROWTH = 1200  # characters one change may add
PER_GROUP = 6  # failures shown per criterion
MAX_TOKENS = 16000  # thinking counts toward it
TARGETS = [*RUBRIC, "card", "code checks"]
MIN_PATIENTS = 3  # distinct patients among the failures a change claims to fix
MIN_CATEGORIES = 2  # ... from this many persona categories (book, cancel, medical, ...)


class Edit(BaseModel):
    target: str
    pattern: str = ""
    cause: str = ""
    failures: list[str]
    elsewhere: list[str] = []
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
            "pattern": {"type": "string"},
            "cause": {"type": "string"},
            "failures": {"type": "array", "items": {"type": "string"}},
            "elsewhere": {"type": "array", "items": {"type": "string"}},
            "reason": {"type": "string"},
            "risk": {"type": "string"},
            "old": {"type": "string"},
            "new": {"type": "string"},
        },
        "required": ["target", "pattern", "cause", "failures", "reason", "risk", "old", "new"],
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


def _groups(d: ReportData, target: str) -> list[FailureGroup]:
    """The failure groups a target covers ("card" covers card_1, card_2, ...)."""
    return [g for g in d.failures if g.id == target]  # card lines are one group, "card"


def _conversation(who: str) -> str:
    return who.split(" · ")[0]  # "aina-late-cancel r1 · card_1: …" -> "aina-late-cancel r1"


def _persona(who: str) -> str:
    return _conversation(who).rsplit(" ", 1)[0]  # "aina-late-cancel r0" -> "aina-late-cancel"


def _lint_rule(evidence: str) -> str | None:
    """The lint rule behind a failure ("turn 1: es: con gusto (Latin American) (...)")."""
    return next((r.id for r in RULES if re.match(rf"turn \d+: {re.escape(r.id)}", evidence)), None)


def check_failures(edit: Edit, d: ReportData, categories: dict[str, str]) -> None:
    """The failures an edit claims are real failures of its target, spread across patients."""
    failed = {_conversation(f.who) for g in _groups(d, edit.target) for f in g.items}
    unknown = [w for w in edit.failures if w not in failed]
    if unknown:
        raise EditRejected(
            f"{', '.join(unknown)} did not fail `{edit.target}` in the visible pass: list only"
            ' conversations from its failures, as "persona rN"'
        )
    patients = {_persona(w) for w in edit.failures}
    kinds = {categories.get(pid, "?") for pid in patients}
    if len(patients) < MIN_PATIENTS or len(kinds) < MIN_CATEGORIES:
        raise EditRejected(
            f"the change covers {len(patients)} patients in {len(kinds)} categories; a pattern"
            f" worth a rule shows in {MIN_PATIENTS}+ patients from {MIN_CATEGORIES}+ categories."
            " Pick a wider pattern (or list all the failures it fixes)"
        )


def failures_brief(
    d: ReportData, categories: dict[str, str] | None = None, per_group: int = PER_GROUP
) -> str:
    """The visible pass as the coach reads it: scores, then per criterion the failures counted
    (conversations, patients, categories), the lint's exact patterns, and the judge's findings."""
    cat = categories or {}
    lines = ["Criteria (pass rate over the visible pass):"]
    lines += [f"- {r.id}: {r.score}" for r in d.criteria]
    for g in d.failures:
        if g.id == "judge errors":
            continue
        patients = sorted({_persona(f.who) for f in g.items})
        kinds = sorted({cat.get(pid, "?") for pid in patients})
        lines += [
            "",
            f"## {g.id}{': ' + g.title if g.title else ''}",
            f"{len(g.items)} conversations fail · {len(patients)} patients ·"
            f" categories: {', '.join(kinds)}",
        ]
        by_rule: dict[str, list[str]] = {}
        judged = []
        for f in g.items:
            rule = _lint_rule(f.evidence) if not f.reason else None
            if rule:
                by_rule.setdefault(rule, []).append(_conversation(f.who))
            else:
                judged.append(f)
        if by_rule:
            lines.append("Exact patterns (lint, counted by code):")
            for rule, whos in sorted(by_rule.items(), key=lambda kv: -len(kv[1])):
                lines.append(f"- `{rule}`: {len(whos)} conversations · {', '.join(whos)}")
        if judged:
            lines.append(f"Judge findings ({min(per_group, len(judged))} of {len(judged)} shown):")
            for f in judged[:per_group]:
                why = f" — {f.reason}" if f.reason else ""
                kind = cat.get(_persona(f.who), "?")
                lines.append(f"- {f.who} [{kind}]: {f.evidence[:400]}{why[:300]}")
            rest = [_conversation(f.who) for f in judged[per_group:]]
            if rest:
                lines.append(f"- also failing: {', '.join(rest)}")
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
    data: ReportData,
    history: str,
    names: set[str],
    categories: dict[str, str],
    attempts: int = 2,
) -> tuple[Edit, str]:
    """One edit and the prompt it makes; a rejected answer is retried once with the reason."""
    brief = failures_brief(data, categories)
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
            candidate = apply_edit(prompt, edit, names)
            check_failures(edit, data, categories)
            return edit, candidate
        except (ValidationError, EditRejected) as err:
            problem = str(err)[:500]
    raise CoachError(f"no valid proposal after {attempts} attempts: {problem}")
