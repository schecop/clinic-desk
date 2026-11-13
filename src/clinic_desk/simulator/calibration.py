"""Calibrating the judge against hand labels.

1. `pick`: choose ~20 conversations of a pass that cover every persona, Catalan and Spanish,
   code passes and failures (deterministic, so a re-run picks the same).
2. `label_template`: a YAML for Pablo, one `yes / no / na` per criterion, with the card lines as
   comments. Labels are synthetic data and are committed (data/eval/judge_labels.yaml).
3. `agreement`: per criterion, how often the judge's label matches the hand label, on all items
   and on the Catalan subset; plus whole-conversation pass/fail agreement and the list of
   disagreements with the judge's reason (what to fix in the judge prompt, or in the labels).
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from clinic_desk.clinic import Lang
from clinic_desk.simulator.judge import Verdict, criteria_for
from clinic_desk.simulator.persona import Persona
from clinic_desk.simulator.runner import Conversation

LABELS = Path("data/eval/judge_labels.yaml")
Label = str  # "yes" | "no" | "na"


def conversation_lang(p: Persona) -> Lang:
    """The language most of the call is in: a switch to Catalan counts as Catalan."""
    return p.language.switch_to or p.language.start


def pick(
    conversations: list[Conversation], personas: dict[str, Persona], n: int = 20
) -> list[Conversation]:
    """Every persona once (repeat 0 first), then fill with code failures and Catalan calls."""
    pool = sorted((c for c in conversations if c.counted), key=lambda c: (c.persona_id, c.repeat))
    chosen: list[Conversation] = []
    seen: set[str] = set()
    for c in pool:
        if c.persona_id not in seen:
            chosen.append(c)
            seen.add(c.persona_id)
    rest = [c for c in pool if c not in chosen]
    rest.sort(
        key=lambda c: (c.code_pass, conversation_lang(personas[c.persona_id]) != "ca", c.persona_id)
    )
    chosen += rest[: max(0, n - len(chosen))]
    return chosen[:n]


def stem(c: Conversation) -> str:
    return f"{c.persona_id}.r{c.repeat}"


def label_template(run_id: str, chosen: list[Conversation], personas: dict[str, Persona]) -> str:
    out = [
        "# Hand labels for judge calibration (synthetic conversations).",
        "# For each criterion write yes / no / na (na: the situation never came up).",
        f"# Read each transcript in runs/sim/{run_id}/<persona>.r<repeat>.txt",
        "# The criteria are defined in src/clinic_desk/simulator/judge.py (RUBRIC).",
        "# Grade the receptionist only; calendar and false claims are checked by code.",
        "",
    ]
    for c in chosen:
        p = personas[c.persona_id]
        out += [
            f"- conversation: {run_id}/{stem(c)}",
            f"  lang: {conversation_lang(p)}",
            f"  code_pass: {str(c.code_pass).lower()}",
            "  labels:",
        ]
        for cid, text in criteria_for(p).items():
            note = f"  # {text}" if cid.startswith("card_") else ""
            out.append(f"    {cid}: {note}".rstrip())
        out += ['  notes: ""', ""]
    return "\n".join(out)


@dataclass(frozen=True)
class Labelled:
    conversation: str
    lang: Lang
    labels: dict[str, Label]


def load_labels(path: Path = LABELS) -> list[Labelled]:
    """Only conversations whose every label is filled in (an unfinished file still works)."""
    raw: list[dict[str, Any]] = yaml.safe_load(path.read_text("utf-8")) or []
    out = []
    for item in raw:
        labels = {k: _norm(v) for k, v in (item.get("labels") or {}).items()}
        if labels and all(v in ("yes", "no", "na") for v in labels.values()):
            out.append(Labelled(item["conversation"], item["lang"], labels))
    return out


def _norm(value: Any) -> str:
    # YAML reads a bare yes/no as a boolean
    if value is True:
        return "yes"
    if value is False:
        return "no"
    return str(value).strip().lower() if value is not None else ""


def group(cid: str) -> str:
    return "card" if cid.startswith("card_") else cid


def agreement(labelled: list[Labelled], verdicts: dict[str, Verdict]) -> str:
    """Markdown: agreement per criterion (all / Catalan), conversation pass, disagreements."""
    hits: dict[str, list[bool]] = defaultdict(list)
    hits_ca: dict[str, list[bool]] = defaultdict(list)
    whole: list[bool] = []
    errors, rows = [], []
    for item in labelled:
        v = verdicts.get(item.conversation)
        if v is None:
            continue
        if v.error:
            errors.append(f"- {item.conversation}: {v.error}")
            continue
        got = v.by_id()
        human_pass = all(label != "no" for label in item.labels.values())
        whole.append(human_pass == v.passed)
        for cid, human in item.labels.items():
            judged = got[cid].label if cid in got else "missing"
            ok = judged == human
            hits[group(cid)].append(ok)
            if item.lang == "ca":
                hits_ca[group(cid)].append(ok)
            if not ok:
                reason = got[cid].reason if cid in got else ""
                rows.append(f"| {item.conversation} | {cid} | {human} | {judged} | {reason} |")

    def rate(xs: list[bool]) -> str:
        return f"{sum(xs)}/{len(xs)} ({sum(xs) / len(xs):.0%})" if xs else "-"

    lines = [
        "| criterion | agreement | Catalan |",
        "|---|---|---|",
        *(f"| {k} | {rate(hits[k])} | {rate(hits_ca[k])} |" for k in sorted(hits)),
        f"| **all criteria** | {rate([x for xs in hits.values() for x in xs])} |"
        f" {rate([x for xs in hits_ca.values() for x in xs])} |",
        f"| **conversation pass/fail** | {rate(whole)} | |",
    ]
    if errors:
        lines += ["", "Judge errors (not scored):", *errors]
    if rows:
        lines += [
            "",
            "| conversation | criterion | human | judge | judge's reason |",
            "|---|---|---|---|---|",
            *rows,
        ]
    return "\n".join(lines)
