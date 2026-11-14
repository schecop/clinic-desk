"""Lint: fixed phrasing rules checked by code on the receptionist's turns, after the call.

The judge grades `language`, `format` and `tone` with judgement; it misses rules that are exact
(Pablo's labels, 2026-11-14): a numbered list, Catalan "li puc ajudar", Latin American "con
gusto", a reminder worded as "li recomano", a booking closed without "anything else?". Code
finds those every time and for free, so a hit fails that criterion whatever the judge said
(`merge`). The judge stays on the same criteria for everything the patterns do not cover.

Only measures; it never rewrites a reply (the coach fixes the prompt).
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from pydantic import BaseModel

from clinic_desk.simulator.judge import CriterionVerdict, Verdict
from clinic_desk.simulator.runner import Conversation


class LintHit(BaseModel):
    turn: int
    criterion: str  # a judge criterion id: language, format, tone
    rule: str
    quote: str

    def __str__(self) -> str:
        return f"turn {self.turn}: {self.rule} ({self.quote!r})"


@dataclass(frozen=True)
class Rule:
    id: str
    criterion: str
    pattern: re.Pattern[str]


def _rx(pattern: str) -> re.Pattern[str]:
    return re.compile(pattern, re.I | re.M)


RULES: tuple[Rule, ...] = (
    Rule("numbered list", "format", _rx(r"^\s*\d+[.)]\s+\S.*$")),
    Rule("bulleted list", "format", _rx(r"^\s*[-*•]\s+\S.*$")),
    Rule("markdown", "format", _rx(r"\*\*[^*]+\*\*|^#+\s")),
    # 'ajudar' and 'registrar' take a direct object: 'la/el puc ajudar', 'l'he de registrar'
    Rule(
        "ca: li + ajudar/registrar",
        "language",
        _rx(
            r"\bli (?:puc |pugui |podem |puguem |he de |hem de |vull )?(?:ajudar|ajudi|registrar)\b"
        ),
    ),
    Rule("ca: tenc (not Barcelona)", "language", _rx(r"\btenc\b")),
    Rule("es: con gusto (Latin American)", "language", _rx(r"\bcon (?:mucho )?gusto\b")),
    Rule(
        "English word",
        "language",
        _rx(r"\b(?:with|pleasure|please|thanks|thank you|sorry|okay|of course)\b"),
    ),
    Rule("ca: em pot + verb (use em podria)", "tone", _rx(r"\bem pot (?:dir|indicar|donar)\b")),
    Rule(
        "reminder worded as a recommendation",
        "tone",
        _rx(r"\b(?:recomano|recomiendo)\b[^.?!\n]{0,40}\b(?:arrib|lleg|port|traig|trae)\w*"),
    ),
)

# an open "anything else?", or an offer of a concrete next step (Pablo, 2026-11-14)
_ANYTHING_ELSE = _rx(
    r"res més|alguna (?:altra )?cosa|algo más|alguna otra cosa|nada más"
    r"|\bvol que (?:li|la|el|l')|\b(?:quiere|desea) que le\b"
)


def lint(c: Conversation) -> list[LintHit]:
    """Every rule on every receptionist turn, plus the closing rule for calls with a write."""
    hits = []
    for t in c.turns:
        for r in RULES:
            for m in r.pattern.finditer(t.desk):
                hits.append(LintHit(turn=t.n, criterion=r.criterion, rule=r.id, quote=m.group(0)))
    first_write = next((t.n for t in c.turns if t.writes), None)
    if first_write is not None and not any(
        _ANYTHING_ELSE.search(t.desk) for t in c.turns if t.n >= first_write
    ):
        last = next((t for t in reversed(c.turns) if t.desk), c.turns[-1])
        hits.append(
            LintHit(
                turn=last.n,
                criterion="tone",
                rule="closes a booking without asking 'anything else?'",
                quote=last.desk[:60],
            )
        )
    return hits


def merge(v: Verdict, hits: list[LintHit]) -> Verdict:
    """The verdict with every criterion a lint hit names failed (judge's reason kept after)."""
    if v.error or not hits:
        return v
    by: dict[str, list[LintHit]] = {}
    for h in hits:
        by.setdefault(h.criterion, []).append(h)
    criteria = []
    for cv in v.criteria:
        found = by.get(cv.id)
        if found:
            cv = CriterionVerdict(
                id=cv.id,
                applies=True,
                passed=False,
                evidence="; ".join(str(h) for h in found),
                reason="lint" + ("" if not cv.passed else f" (judge passed it: {cv.reason})"),
            )
        criteria.append(cv)
    return v.model_copy(
        update={"criteria": criteria, "lint": [f"{h.criterion}: {h}" for h in hits]}
    )
