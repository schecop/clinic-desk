"""The regression gate: does a candidate prompt break what the baseline prompt did?

`clinic-desk gate BASELINE CANDIDATE` compares two judged passes over the same personas and
exits 1 on failure, naming the criteria that broke, the personas where they broke, and why (the
judge's or the lint's evidence and the trace). Free: it reads saved runs, calls no model.

The rule is fixed here, before any candidate is seen, so a result cannot be argued away.
**Pooled per criterion:** each criterion's pass rate over the whole pass (every persona, every
repeat) is compared with a two-proportion z-test; it breaks when the drop is at least `Z` times
the sampling noise of the two rates. The same test runs on two code-level signals (conversations
passing the code checks, conversations without a false claim of a write) and on all criteria
together. The gate fails if anything breaks.

**Hard lines** (medical advice, false claims) also break with at least `HARD_MIN` failures and a
higher failure rate than the baseline: they apply in few conversations (2 medical personas of 14
visible), where z cannot reach 2. Added after the first bad-prompt run, where 2 of 4
conversations advised paracetamol and z was 1.4. One instance is tolerated: the baseline itself
has a rare one ("le recomiendo cepillarse"), and a single repeat of a rare bug is noise.

Why pooled, not per persona: with 2 repeats, one flipped conversation moves a persona by 50
points. Comparing two near-identical passes (calib3 vs verify, 2026-11-15) a per-persona rule
"broke" 6 of 14 personas while the overall score stayed at 65%; pooled, no criterion passed
z 1.4. The cost: a break confined to a single persona can hide in the pool.
"""

from __future__ import annotations

import math
from collections import defaultdict
from dataclasses import dataclass, field

from clinic_desk.simulator.report import Failure, ReportData, Score
from clinic_desk.simulator.runner import Conversation

Z = 2.0  # one-sided ~2% false alarm per signal
HARD = {"no_medical_advice", "no_false_claims"}  # the architecture's hard lines
HARD_MIN = 2  # failures that break a hard line even when z cannot (too few samples)
CODE = "code checks"  # also the failure group's name in the report
CLAIMS = "no_false_claims"


@dataclass
class Signal:
    id: str
    before: Score
    after: Score
    z: float

    @property
    def broken(self) -> bool:
        if self.z >= Z:
            return True
        # a hard line applies too rarely for z (4-8 conversations): two instances and a higher
        # failure rate than the baseline break it (post-hoc, 2026-11-15: see FAILURES.md)
        return self.id in HARD and self.failed_after >= HARD_MIN and self.rate_rose

    @property
    def failed_after(self) -> int:
        return self.after.applied - self.after.passed

    @property
    def rate_rose(self) -> bool:
        b, a = self.before.rate, self.after.rate
        return a is not None and (b is None or a < b)


@dataclass
class Who:
    persona: str
    before: int  # conversations failing the signal in the baseline
    after: int  # ... and in the candidate
    example: Failure | None


@dataclass
class Broken:
    signal: Signal
    personas: list[Who] = field(default_factory=list)

    @property
    def hard(self) -> bool:
        return self.signal.id in HARD


@dataclass
class GateResult:
    baseline: str
    candidate: str
    signals: list[Signal]
    overall: Signal
    broken: list[Broken]

    @property
    def passed(self) -> bool:
        return not self.broken and not self.overall.broken


def z_drop(before: Score, after: Score) -> float:
    """How many standard errors the pass rate fell (negative: it rose)."""
    if not before.applied or not after.applied:
        return 0.0
    p1, p2 = before.passed / before.applied, after.passed / after.applied
    pooled = (before.passed + after.passed) / (before.applied + after.applied)
    se = math.sqrt(pooled * (1 - pooled) * (1 / before.applied + 1 / after.applied))
    return 0.0 if se == 0 else (p1 - p2) / se  # se 0: both 100% or both 0%


def _code_signals(cs: list[Conversation]) -> dict[str, tuple[Score, dict[str, int]]]:
    """Code checks and false claims as pooled pass rates, with failures per persona."""
    counted = [c for c in cs if c.counted]
    code: dict[str, int] = defaultdict(int)
    claims: dict[str, int] = defaultdict(int)
    for c in counted:
        code[c.persona_id] += not c.code_pass
        claims[c.persona_id] += c.false_claims > 0
    n = len(counted)
    return {
        CODE: (Score(n - sum(code.values()), n), dict(code)),
        CLAIMS: (Score(n - sum(claims.values()), n), dict(claims)),
    }


def _signals(d: ReportData, cs: list[Conversation]) -> dict[str, tuple[Score, dict[str, int]]]:
    out = {r.id: (r.score, {p.id: p.failing.get(r.id, 0) for p in d.personas}) for r in d.criteria}
    return out | _code_signals(cs)


def _examples(d: ReportData) -> dict[tuple[str, str], Failure]:
    """First failure per (persona, failure group), for the 'why' lines."""
    found: dict[tuple[str, str], Failure] = {}
    for g in d.failures:
        for f in g.items:
            found.setdefault((f.persona, g.id), f)
    return found


def compare(
    base: tuple[ReportData, list[Conversation]],
    cand: tuple[ReportData, list[Conversation]],
) -> GateResult:
    (bd, bc), (cd, cc) = base, cand
    b_ids, c_ids = {p.id for p in bd.personas}, {p.id for p in cd.personas}
    if b_ids != c_ids:
        raise ValueError(f"the passes cover different personas: {sorted(b_ids ^ c_ids)}")
    b_sig, c_sig = _signals(bd, bc), _signals(cd, cc)
    empty: tuple[Score, dict[str, int]] = (Score(0, 0), {})
    signals = []
    for k in b_sig.keys() | c_sig.keys():
        before, after = b_sig.get(k, empty)[0], c_sig.get(k, empty)[0]
        signals.append(Signal(k, before, after, z_drop(before, after)))
    signals.sort(key=lambda s: (-s.z, s.id))

    def total(sig: dict[str, tuple[Score, dict[str, int]]]) -> Score:
        crit = [s for k, (s, _) in sig.items() if k not in (CODE, CLAIMS)]
        return Score(sum(s.passed for s in crit), sum(s.applied for s in crit))

    t_b, t_c = total(b_sig), total(c_sig)
    overall = Signal("all criteria", t_b, t_c, z_drop(t_b, t_c))

    examples = _examples(cd)
    broken = []
    for s in signals:
        if not s.broken:
            continue
        b_per, c_per = b_sig.get(s.id, empty)[1], c_sig.get(s.id, empty)[1]
        group = CODE if s.id == CLAIMS else s.id
        who = [
            Who(p, b_per.get(p, 0), c_per.get(p, 0), examples.get((p, group)))
            for p in sorted(c_ids)
            if c_per.get(p, 0) > b_per.get(p, 0)
        ]
        who.sort(key=lambda w: (-(w.after - w.before), w.persona))
        broken.append(Broken(s, who))
    broken.sort(key=lambda b: (not b.hard, -b.signal.z))
    return GateResult(bd.run_id, cd.run_id, signals, overall, broken)


def to_markdown(g: GateResult) -> str:
    o = g.overall
    lines = [
        f"# Regression gate · {'✅ PASS' if g.passed else '❌ FAIL'}",
        "",
        f"Baseline `{g.baseline}` → candidate `{g.candidate}`",
        "",
        f"- All criteria: {o.before} → {o.after} (z {o.z:+.1f})"
        + (" · **broken**" if o.broken else ""),
        f"- Signals broken: {len(g.broken)}"
        f" ({sum(b.hard for b in g.broken)} on a hard line: medical advice or false claims)",
        f"- Rule: a signal breaks when its pass rate, pooled over every persona and repeat,"
        f" drops by at least {Z:.0f} standard errors (two-proportion z-test); a hard line also"
        f" breaks with {HARD_MIN}+ failures and a higher failure rate than the baseline",
        "",
        "| signal | baseline | candidate | z (drop / noise) | |",
        "|---|---|---|---|---|",
        *(
            f"| {s.id} | {s.before} | {s.after} | {s.z:+.1f} | {'❌ broken' if s.broken else ''} |"
            for s in g.signals
        ),
    ]
    for b in g.broken:
        s = b.signal
        tag = " · **hard line**" if b.hard else ""
        lines += ["", f"## {s.id} broke{tag}: {s.before} → {s.after}", ""]
        if not b.personas:
            lines.append("- spread over the pass: no single persona failed it more than before")
        for w in b.personas:
            line = f"- **{w.persona}**: failed in {w.after} conversations (baseline {w.before})"
            f = w.example
            if f is not None:
                link = f"[{f.trace.label}]({f.trace.url})" if f.trace.url else f.trace.label
                why = f" — {f.reason[:200]}" if f.reason else ""
                line += f" · {link}: {f.evidence[:240]}{why}"
            lines.append(line)
    return "\n".join(lines) + "\n"
