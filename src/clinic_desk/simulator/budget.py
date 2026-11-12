"""The $15 cap, enforced in code: a cost ledger, an estimate before each run, and a refusal.

Every paid call of a run is appended to `runs/cost.jsonl` (gitignored) with its tokens and its
cost at list price. Before a run, the runner estimates its cost from fixed per-conversation
assumptions (docs/model-research.md) and refuses to start if spent + estimate would pass the cap;
it warns from 80%. The estimate is rough on purpose: the ledger is the truth, and every report
prints both so the assumptions can be corrected.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

from clinic_desk.llm.metered import MTOK, Call

LEDGER = Path("runs/cost.jsonl")
CAP_USD = Decimal(15)
WARN_AT = Decimal("0.8")


@dataclass(frozen=True)
class Usage:
    """Assumed calls per conversation for a role, and tokens per call (model-research.md)."""

    calls: int
    tokens_in: int
    tokens_out: int


# one conversation: 10 receptionist calls x ~3k in / 200 out; 8 persona calls x 1.5k / 100;
# the translator only on wrong-language turns (assume 1); the judge once x 5k / 500
PER_CONVERSATION: dict[str, Usage] = {
    "receptionist": Usage(10, 3000, 200),
    "translator": Usage(1, 300, 150),
    "persona": Usage(8, 1500, 100),
    "judge": Usage(1, 5000, 500),
}


class BudgetExceeded(RuntimeError):
    pass


def estimate(conversations: int, prices: Mapping[str, tuple[Decimal, Decimal]]) -> Decimal:
    """Cost of `conversations` for the roles in `prices` (role -> usd per Mtok in, out)."""
    total = Decimal(0)
    for role, (p_in, p_out) in prices.items():
        u = PER_CONVERSATION[role]
        total += u.calls * (u.tokens_in * p_in + u.tokens_out * p_out) / MTOK
    return total * conversations


def spent(ledger: Path = LEDGER) -> Decimal:
    if not ledger.exists():
        return Decimal(0)
    return sum(
        (Decimal(json.loads(line)["usd"]) for line in ledger.read_text("utf-8").splitlines()),
        Decimal(0),
    )


def check(planned: Decimal, ledger: Path = LEDGER, cap: Decimal = CAP_USD) -> str:
    """A line to print before the run; raises if the run would pass the cap."""
    already = spent(ledger)
    after = already + planned
    line = f"budget: spent ${already:.4f} + this run ~${planned:.4f} = ${after:.4f} of ${cap}"
    if after > cap:
        raise BudgetExceeded(line + " — refusing to start")
    if after > cap * WARN_AT:
        line += f" — WARNING: over {WARN_AT:.0%} of the cap"
    return line


def record(calls: Iterable[Call], run_id: str, ledger: Path = LEDGER) -> Decimal:
    """Append the paid calls (cost > 0) to the ledger; returns their total."""
    now = datetime.now(UTC).isoformat(timespec="seconds")
    lines, total = [], Decimal(0)
    for c in calls:
        if c.usd <= 0:
            continue
        total += c.usd
        lines.append(
            json.dumps(
                {
                    "at": now,
                    "run": run_id,
                    "role": c.role,
                    "provider": c.provider,
                    "model": c.model,
                    "in": c.input_tokens,
                    "out": c.output_tokens,
                    "usd": str(c.usd),
                }
            )
        )
    if lines:
        ledger.parent.mkdir(parents=True, exist_ok=True)
        with ledger.open("a", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")
    return total
