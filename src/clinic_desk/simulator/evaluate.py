"""Score a pass: the judge on every counted conversation, with the code lint merged in.

Used by `simulate` (right after the pass) and by `judge` (on a saved pass). Each verdict is
saved next to its conversation as `<persona>.r<n>.judge-<role>.json`, so a report or the
regression gate can read a pass back without calling a model again.
"""

from __future__ import annotations

import asyncio
from decimal import Decimal
from pathlib import Path
from typing import Literal

from clinic_desk.clinic import Clinic
from clinic_desk.llm.base import LLMClient, LLMError
from clinic_desk.llm.metered import Call, Metered
from clinic_desk.simulator.calibration import stem
from clinic_desk.simulator.judge import Verdict, judge
from clinic_desk.simulator.lint import lint, merge
from clinic_desk.simulator.persona import Persona
from clinic_desk.simulator.runner import Conversation
from clinic_desk.simulator.traces import build_judge_trace
from clinic_desk.tracing import LangfuseExporter


def verdict_path(folder: Path, c: Conversation, role: str) -> Path:
    return folder / f"{stem(c)}.judge-{role}.json"


async def judge_pass(
    conversations: list[Conversation],
    llm: LLMClient,
    *,
    run_id: str,
    folder: Path,
    role: str,
    personas: dict[str, Persona],
    clinic: Clinic,
    mode: Literal["tool", "json"] = "tool",
    concurrency: int = 4,
    exporter: LangfuseExporter | None = None,
) -> tuple[dict[str, Verdict], dict[str, Verdict]]:
    """(judge + lint, judge alone), keyed by "<run>/<persona>.r<n>". Counted conversations only.

    With an exporter, each verdict and its model calls join the conversation's Langfuse trace.
    """
    gate = asyncio.Semaphore(concurrency)
    merged: dict[str, Verdict] = {}
    alone: dict[str, Verdict] = {}

    async def one(c: Conversation) -> None:
        name = f"{run_id}/{stem(c)}"
        mine: list[Call] = []  # this conversation's judge calls, for its trace
        prices = llm.prices if isinstance(llm, Metered) else (Decimal(0), Decimal(0))
        metered = Metered(llm, "judge", *prices, calls=mine)
        async with gate:
            try:
                raw = await judge(metered, c, personas[c.persona_id], clinic, name, mode=mode)
            except LLMError as err:
                raw = Verdict(conversation=name, model=llm.model, criteria=[], error=str(err))
        alone[name] = raw
        merged[name] = v = merge(raw, lint(c))
        verdict_path(folder, c, role).write_text(v.model_dump_json(indent=1), "utf-8")
        if exporter is not None and c.trace_id:
            await exporter.export(build_judge_trace(c.trace_id, v, mine))  # never fatal

    await asyncio.gather(*(one(c) for c in conversations if c.counted))
    return merged, alone


def load_verdicts(run_id: str, role: str, folder: Path) -> dict[str, Verdict]:
    """The saved verdicts of one judge role for a pass, keyed like `judge_pass`."""
    found = {}
    for f in sorted(folder.glob(f"*.judge-{role}.json")):
        v = Verdict.model_validate_json(f.read_text("utf-8"))
        found[v.conversation] = v
    return found
