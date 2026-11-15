"""The coach loop: propose one change, measure it, keep it or roll it back, log why.

Each iteration:
1. The coach proposes one edit from the visible failures of the current best prompt.
2. A visible pass measures the candidate (14 personas x repeats, judged).
3. Keep rule, fixed before the run (Pablo, 2026-11-15): the **target criterion rises by z ≥ 2**
   over the best prompt's visible pass, the regression gate sees **no regression on visible**,
   and only then a held-out pass, where the gate must see **no regression on held-out**.
4. Kept: the candidate becomes the best prompt, and its passes the new baselines. Rolled back:
   the best prompt stays. Either way the change log gets the diff, the reason, the scores and
   the decision.

Passes and the model are injected (`Evaluate`, `Propose`), so the loop runs in tests with fakes.
The coach's view of the history (`history_for_coach`) never carries held-out numbers.
"""

from __future__ import annotations

import difflib
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from pydantic import BaseModel

from clinic_desk.coach.propose import CoachError, Edit
from clinic_desk.simulator.gate import GateResult, Z, compare
from clinic_desk.simulator.report import ReportData
from clinic_desk.simulator.runner import Conversation

Split = Literal["visible", "held_out"]


@dataclass
class Pass:
    run_id: str
    data: ReportData
    conversations: list[Conversation]

    @property
    def pair(self) -> tuple[ReportData, list[Conversation]]:
        return self.data, self.conversations


Evaluate = Callable[[str, Split, str], Awaitable[Pass]]  # prompt, split, label -> judged pass
Propose = Callable[[str, ReportData, str], Awaitable[tuple[Edit, str]]]  # prompt, brief, history


class Decision(BaseModel):
    kept: bool
    why: str
    target_z: float  # rise of the target criterion on visible, in standard errors


class Entry(BaseModel):
    iteration: int
    target: str = ""
    reason: str = ""
    risk: str = ""
    failures: list[str] = []
    diff: str = ""
    visible_run: str = ""
    held_out_run: str = ""
    target_before: str = ""
    target_after: str = ""
    visible_before: str = ""
    visible_after: str = ""
    held_out_before: str = ""
    held_out_after: str = ""
    kept: bool = False
    why: str = ""


def _broken(g: GateResult) -> str:
    ids = [b.signal.id for b in g.broken] + (["all criteria"] if g.overall.broken else [])
    return ", ".join(ids)


def decide_visible(target: str, base: Pass, cand: Pass) -> tuple[Decision, GateResult]:
    g = compare(base.pair, cand.pair)
    signal = next((s for s in g.signals if s.id == target), None)
    rise = -signal.z if signal else 0.0
    if rise < Z:
        why = f"target {target} rose z {rise:+.1f}, under {Z:.0f}: not beyond the noise"
        return Decision(kept=False, why=why, target_z=rise), g
    if not g.passed:
        return Decision(kept=False, why=f"visible regression: {_broken(g)}", target_z=rise), g
    return Decision(kept=True, why=f"target {target} rose z {rise:+.1f}", target_z=rise), g


def decide_held_out(visible: Decision, base: Pass, cand: Pass) -> tuple[Decision, GateResult]:
    g = compare(base.pair, cand.pair)
    if not g.passed:
        why = f"{visible.why}; held-out regression: {_broken(g)}"
        return Decision(kept=False, why=why, target_z=visible.target_z), g
    why = f"{visible.why}; held-out: no regression"
    return Decision(kept=True, why=why, target_z=visible.target_z), g


def diff(before: str, after: str) -> str:
    return "".join(
        difflib.unified_diff(
            before.splitlines(keepends=True),
            after.splitlines(keepends=True),
            "receptionist.md (best)",
            "receptionist.md (candidate)",
            n=1,
        )
    )


def history_for_coach(entries: list[Entry]) -> str:
    """Earlier attempts as the coach sees them: no held-out run, score or persona."""
    out = []
    for e in entries:
        if not e.target:
            out.append(f"{e.iteration}. no valid proposal ({e.why})")
            continue
        verdict = "KEPT" if e.kept else "ROLLED BACK"
        why = e.why.split("; held-out")[0]
        if not e.kept and "held-out regression" in e.why:
            why += "; it regressed on patients you do not see"
        out.append(
            f"{e.iteration}. {verdict} · target {e.target}: {e.target_before} → {e.target_after}"
            f" · {why}\n   reason: {e.reason}\n   change:\n{_indent(e.diff)}"
        )
    return "\n".join(out)


def _indent(text: str) -> str:
    return "\n".join("     " + line for line in text.splitlines())


def _score(p: Pass, signal: str) -> str:
    if signal == "all":
        rows = [s for s in p.data.splits if s.split != "all"]
        passed, applied = (
            sum(s.criteria.passed for s in rows),
            sum(s.criteria.applied for s in rows),
        )
        return f"{passed / applied:.0%} ({passed}/{applied})" if applied else "-"
    row = next((r for r in p.data.criteria if r.id == signal), None)
    return str(row.score) if row else "-"


def to_markdown(entries: list[Entry], start: str, best: str) -> str:
    kept = sum(e.kept for e in entries)
    lines = [
        "# Coach change log",
        "",
        f"{len(entries)} iterations · {kept} kept · keep rule: target criterion z ≥ {Z:.0f} on"
        " visible, no regression on visible (gate), no regression on held-out (gate)",
        "",
        "| # | target | target before → after | visible all | held-out all | decision |",
        "|---|---|---|---|---|---|",
    ]
    for e in entries:
        ho = f"{e.held_out_before} → {e.held_out_after}" if e.held_out_after else "not run"
        lines.append(
            f"| {e.iteration} | {e.target or '-'} | {e.target_before} → {e.target_after} |"
            f" {e.visible_before} → {e.visible_after} | {ho} |"
            f" {'✅ kept' if e.kept else '↩️ rolled back'} |"
        )
    for e in entries:
        lines += ["", f"## {e.iteration} · {e.target or 'no proposal'}", ""]
        lines += [f"- **Decision:** {'kept' if e.kept else 'rolled back'} — {e.why}"]
        if e.target:
            lines += [
                f"- **Reason:** {e.reason}",
                f"- **Risk (coach):** {e.risk}",
                f"- **Targets:** {', '.join(e.failures)}",
                f"- **Runs:** visible `{e.visible_run}`"
                + (f", held-out `{e.held_out_run}`" if e.held_out_run else ""),
                "",
                "```diff",
                e.diff.rstrip(),
                "```",
            ]
    lines += [
        "",
        "## Start → best",
        "",
        "```diff",
        diff(start, best).rstrip() or "(no change)",
        "```",
    ]
    return "\n".join(lines) + "\n"


async def coach_loop(
    start: str,
    *,
    propose: Propose,
    evaluate: Evaluate,
    iterations: int,
    out: Path,
    visible0: Pass | None = None,
    held_out0: Pass | None = None,
) -> tuple[str, list[Entry]]:
    """Run the loop; write changelog.jsonl/.md, best.md and each candidate into `out`."""
    out.mkdir(parents=True, exist_ok=True)
    (out / "start.md").write_text(start, "utf-8")
    best = start
    vis = visible0 or await evaluate(best, "visible", "coach-v0")
    # measured only when a change first wins on visible (until then `best` is still `start`)
    ho = held_out0
    entries: list[Entry] = []
    log = out / "changelog.jsonl"

    def save(entry: Entry) -> None:
        entries.append(entry)
        with log.open("a", encoding="utf-8") as f:
            f.write(entry.model_dump_json() + "\n")
        (out / "changelog.md").write_text(to_markdown(entries, start, best), "utf-8")

    for i in range(1, iterations + 1):
        try:
            edit, candidate = await propose(best, vis.data, history_for_coach(entries))
        except CoachError as err:
            save(Entry(iteration=i, why=str(err)))
            continue
        (out / f"candidate.{i}.md").write_text(candidate, "utf-8")
        cv = await evaluate(candidate, "visible", f"coach-v{i}")
        decision, _ = decide_visible(edit.target, vis, cv)
        ch: Pass | None = None
        if decision.kept:
            if ho is None:
                ho = await evaluate(best, "held_out", "coach-h0")
            ch = await evaluate(candidate, "held_out", f"coach-h{i}")
            decision, _ = decide_held_out(decision, ho, ch)
        entry = Entry(
            iteration=i,
            target=edit.target,
            reason=edit.reason,
            risk=edit.risk,
            failures=edit.failures,
            diff=diff(best, candidate),
            visible_run=cv.run_id,
            held_out_run=ch.run_id if ch else "",
            target_before=_score(vis, edit.target),
            target_after=_score(cv, edit.target),
            visible_before=_score(vis, "all"),
            visible_after=_score(cv, "all"),
            held_out_before=_score(ho, "all") if ch and ho else "",
            held_out_after=_score(ch, "all") if ch else "",
            kept=decision.kept,
            why=decision.why,
        )
        if decision.kept:
            assert ch is not None
            best, vis, ho = candidate, cv, ch
        save(entry)
    (out / "best.md").write_text(best, "utf-8")
    (out / "changelog.md").write_text(to_markdown(entries, start, best), "utf-8")
    return best, entries
