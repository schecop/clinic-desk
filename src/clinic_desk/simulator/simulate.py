"""A simulator pass: personas x repeats, in parallel, saved to `runs/sim/<run_id>/`.

Each conversation is saved as JSON (for the judge and the report) and as a readable transcript.
Paid calls go to the cost ledger as each conversation ends, so a pass stopped half-way still
counts what it spent. The summary gives what code can score today: code pass rate, end reasons,
false claims, language misses, cost and receptionist latency per turn.
"""

from __future__ import annotations

import asyncio
import os
import statistics
from collections import Counter
from collections.abc import Callable
from datetime import datetime
from decimal import Decimal
from pathlib import Path

from clinic_desk.simulator import budget
from clinic_desk.simulator.persona import Persona
from clinic_desk.simulator.runner import Conversation, RoleModel, run_conversation, transcript
from clinic_desk.simulator.traces import build_trace
from clinic_desk.tracing import LangfuseExporter

RUNS = Path("runs/sim")


def new_run_id(label: str = "") -> str:
    return datetime.now().strftime("%Y%m%d-%H%M%S") + (f"-{label}" if label else "")


async def run_pass(
    personas: list[Persona],
    *,
    receptionist: RoleModel,
    persona_model: RoleModel,
    translator: RoleModel | None = None,
    repeats: int = 1,
    concurrency: int = 4,
    run_id: str | None = None,
    out: Path = RUNS,
    ledger: Path = budget.LEDGER,
    exporter: LangfuseExporter | None = None,
    split_of: dict[str, str] | None = None,  # persona id -> "visible" / "held_out", for tags
) -> tuple[str, list[Conversation]]:
    run_id = run_id or new_run_id()
    folder = out / run_id
    folder.mkdir(parents=True, exist_ok=True)
    gate = asyncio.Semaphore(concurrency)

    async def one(p: Persona, r: int) -> Conversation:
        async with gate:
            c = await run_conversation(
                p,
                receptionist=receptionist,
                persona_model=persona_model,
                translator=translator,
                repeat=r,
            )
        if exporter is not None:
            trace = build_trace(c, run_id, (split_of or {}).get(p.id, ""))
            if await exporter.export(trace):  # a failed export is logged, never fatal
                c.trace_id = trace.trace_id
        stem = f"{p.id}.r{r}"
        (folder / f"{stem}.json").write_text(c.model_dump_json(indent=1), "utf-8")
        (folder / f"{stem}.txt").write_text(transcript(c) + "\n", "utf-8")
        budget.record(c.calls, run_id, ledger)
        return c

    jobs = [one(p, r) for p in personas for r in range(repeats)]
    conversations = list(await asyncio.gather(*jobs))
    text = summary(run_id, conversations, langfuse_trace_url())
    (folder / "summary.md").write_text(text + "\n", "utf-8")
    return run_id, conversations


def _pct(values: list[float], q: float) -> float:
    if not values:
        return 0.0
    if len(values) == 1:
        return values[0]
    return statistics.quantiles(values, n=100, method="inclusive")[int(q) - 1]


def summary(
    run_id: str,
    conversations: list[Conversation],
    trace_url: Callable[[str], str] | None = None,
) -> str:
    counted = [c for c in conversations if c.counted]
    cost = sum((c.usd for c in conversations), Decimal(0))
    seconds = [t.seconds for c in conversations for t in c.turns if t.desk]
    ends = Counter(c.end for c in conversations)
    translated = sum(t.lang_fix == "translated" for c in conversations for t in c.turns)
    p50, p95 = _pct(seconds, 50), _pct(seconds, 95)
    lines = [
        f"# Simulator pass {run_id}",
        "",
        f"- Conversations: {len(conversations)} ({len(counted)} counted;"
        f" ends: {', '.join(f'{k} {v}' for k, v in sorted(ends.items()))})",
        f"- Code pass: {sum(c.code_pass for c in counted)}/{len(counted)}"
        " (done, calendar right, no false claim, right language)",
        f"- False claims: {sum(c.false_claims for c in conversations)}"
        f" · language misses: {sum(c.language_misses for c in conversations)}"
        f" · translations: {translated}",
        f"- Cost: ${cost:.4f} (${cost / max(len(conversations), 1):.4f} per conversation)",
        f"- Receptionist latency per turn: p50 {p50:.1f}s · p95 {p95:.1f}s",
        "",
        "| persona | category | end | calendar | false claims | lang misses | turns | code pass"
        " | trace |",
        "|---|---|---|---|---|---|---|---|---|",
    ]
    for c in sorted(conversations, key=lambda c: (c.persona_id, c.repeat)):
        trace = "-"
        if c.trace_id:
            trace = f"[{c.trace_id[:8]}]({trace_url(c.trace_id)})" if trace_url else c.trace_id[:8]
        lines.append(
            f"| {c.persona_id} r{c.repeat} | {c.category} | {c.end} | {c.calendar_ok} |"
            f" {c.false_claims} | {c.language_misses} | {len(c.turns)} |"
            f" {'✅' if c.code_pass else '❌'} | {trace} |"
        )
    return "\n".join(lines)


def langfuse_trace_url() -> Callable[[str], str] | None:
    """Trace links need the project id (LANGFUSE_PROJECT_ID in .env, from the project's URL)."""
    project = os.environ.get("LANGFUSE_PROJECT_ID")
    if not project:
        return None
    host = (os.environ.get("LANGFUSE_HOST") or "https://cloud.langfuse.com").rstrip("/")
    return lambda trace_id: f"{host}/project/{project}/traces/{trace_id}"
