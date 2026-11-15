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

from clinic_desk.receptionist.graph import load_prompt
from clinic_desk.receptionist.language import clear_lang
from clinic_desk.simulator import budget
from clinic_desk.simulator.judge import Verdict
from clinic_desk.simulator.persona import Persona
from clinic_desk.simulator.runner import Conversation, RoleModel, run_conversation, transcript
from clinic_desk.simulator.traces import build_trace
from clinic_desk.tracing import LangfuseExporter

RUNS = Path("runs/sim")


def load_run(run_id: str, out: Path = RUNS) -> list[Conversation]:
    """The saved conversations of a pass (`<persona>.r<n>.json`; judge files are skipped)."""
    folder = out / run_id
    if not folder.is_dir():
        raise FileNotFoundError(f"no pass {run_id!r} in {out}")
    return [
        Conversation.model_validate_json(f.read_text("utf-8"))
        for f in sorted(folder.glob("*.r[0-9]*.json"))
        if ".judge-" not in f.name
    ]


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
    prompt: str | None = None,  # the receptionist prompt; None = prompts/receptionist.md
    first_repeat: int = 0,  # > 0 adds repeats to an existing pass (the coach's sequential test)
) -> tuple[str, list[Conversation]]:
    run_id = run_id or new_run_id()
    folder = out / run_id
    folder.mkdir(parents=True, exist_ok=True)
    prompt = prompt if prompt is not None else load_prompt()
    (folder / "prompt.md").write_text(prompt, "utf-8")  # what this pass measured, for gate + coach
    gate = asyncio.Semaphore(concurrency)

    async def one(p: Persona, r: int) -> Conversation:
        async with gate:
            c = await run_conversation(
                p,
                receptionist=receptionist,
                persona_model=persona_model,
                translator=translator,
                prompt=prompt,
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

    jobs = [one(p, r) for p in personas for r in range(first_repeat, first_repeat + repeats)]
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


def _spoken(c: Conversation) -> str:
    """The language(s) the patient actually spoke in a conversation."""
    found = {lang for t in c.turns if (lang := clear_lang(t.patient))}
    if len(found) > 1:
        return "both"
    return {"es": "Spanish", "ca": "Catalan"}.get(next(iter(found), ""), "unclear")


def summary(
    run_id: str,
    conversations: list[Conversation],
    trace_url: Callable[[str], str] | None = None,
    verdicts: dict[str, Verdict] | None = None,  # "<run>/<persona>.r<n>" -> judge + lint
) -> str:
    counted = [c for c in conversations if c.counted]
    cost = sum((c.usd for c in conversations), Decimal(0))
    seconds = [t.seconds for c in conversations for t in c.turns if t.desk]
    ends = Counter(c.end for c in conversations)
    translated = sum(t.lang_fix == "translated" for c in conversations for t in c.turns)
    spoken = Counter(_spoken(c) for c in counted)
    off_card = sorted(
        {c.persona_id for c in counted if not all(t.persona_lang_ok for t in c.turns)}
    )
    p50, p95 = _pct(seconds, 50), _pct(seconds, 95)
    lines = [
        f"# Simulator pass {run_id}",
        "",
        f"- Conversations: {len(conversations)} ({len(counted)} counted;"
        f" ends: {', '.join(f'{k} {v}' for k, v in sorted(ends.items()))})",
        f"- Code pass: {sum(c.code_pass for c in counted)}/{len(counted)}"
        " (done, calendar right, no false claim, right language)",
        f"- False claims: {sum(c.false_claims for c in conversations)}"
        f" · claims blocked by the guard: {sum(c.claims_blocked for c in conversations)}"
        f" · language misses: {sum(c.language_misses for c in conversations)}"
        f" · translations: {translated}",
        # the patient is free to change language: measured, not enforced, so we know what was
        # tested (FAILURES: Spanish cards drifting to Catalan)
        "- Patients spoke: "
        + " · ".join(f"{k} {spoken[k]}" for k in ("Spanish", "Catalan", "both", "unclear"))
        + f" · off their card: {', '.join(off_card) or 'none'}",
        f"- Cost: ${cost:.4f} (${cost / max(len(conversations), 1):.4f} per conversation)",
        f"- Receptionist latency per turn: p50 {p50:.1f}s · p95 {p95:.1f}s",
    ]
    judged = verdicts or {}

    def verdict(c: Conversation) -> Verdict | None:
        return judged.get(f"{run_id}/{c.persona_id}.r{c.repeat}")

    if judged:
        ok = [v for v in judged.values() if not v.error]
        failing = Counter(cv.id for v in ok for cv in v.criteria if cv.applies and not cv.passed)
        model = next(iter(judged.values())).model
        applied = [cv for v in ok for cv in v.criteria if cv.applies]
        rate = sum(cv.passed for cv in applied) / max(len(applied), 1)
        lines += [
            # a whole conversation rarely passes every criterion; this one moves with each fix
            f"- **Criteria passed: {sum(cv.passed for cv in applied)}/{len(applied)}"
            f" ({rate:.0%})** (judge + lint, criteria that applied)",
            f"- Judge ({model}, with the lint): {sum(v.passed for v in ok)}/{len(ok)} pass"
            f" · errors {len(judged) - len(ok)} · failing criteria:"
            f" {', '.join(f'{k} {n}' for k, n in failing.most_common()) or 'none'}",
        ]
    lines += [
        "",
        "| persona | category | end | calendar | false claims | lang misses | turns | code pass"
        " | judge | trace |",
        "|---|---|---|---|---|---|---|---|---|---|",
    ]
    for c in sorted(conversations, key=lambda c: (c.persona_id, c.repeat)):
        trace = "-"
        if c.trace_id:
            trace = f"[{c.trace_id[:8]}]({trace_url(c.trace_id)})" if trace_url else c.trace_id[:8]
        lines.append(
            f"| {c.persona_id} r{c.repeat} | {c.category} | {c.end} | {c.calendar_ok} |"
            f" {c.false_claims} | {c.language_misses} | {len(c.turns)} |"
            f" {'✅' if c.code_pass else '❌'} | {_judge_cell(verdict(c))} | {trace} |"
        )
    return "\n".join(lines)


def _judge_cell(v: Verdict | None) -> str:
    if v is None:
        return "-"
    if v.error:
        return "error"
    failed = [cv.id for cv in v.criteria if cv.applies and not cv.passed]
    return "✅" if not failed else "❌ " + ", ".join(failed)


def langfuse_trace_url() -> Callable[[str], str] | None:
    """Trace links need the project id (LANGFUSE_PROJECT_ID in .env, from the project's URL)."""
    project = os.environ.get("LANGFUSE_PROJECT_ID")
    if not project:
        return None
    host = (os.environ.get("LANGFUSE_HOST") or "https://cloud.langfuse.com").rstrip("/")
    return lambda trace_id: f"{host}/project/{project}/traces/{trace_id}"
