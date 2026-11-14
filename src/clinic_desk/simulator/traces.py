"""One Langfuse trace per simulated conversation, built when it ends from what was timed.

    trace "sim <persona>"          session = the pass (run id); tags: category, end, pass/fail
    ├─ turn 1                      patient in, receptionist out; WARNING if a check failed
    │  ├─ persona (generation)     model, tokens, cost
    │  ├─ receptionist (generation)
    │  ├─ identify_patient (tool)  arguments in, result out; ERROR if the tool refused
    │  └─ …
    └─ turn 2 …

Attribute names follow Langfuse's OpenTelemetry mapping (langfuse.com/integrations/native/
opentelemetry): `langfuse.observation.type`, `.input`/`.output` and `.usage_details`/
`.cost_details` as JSON strings, `langfuse.session.id`, `langfuse.trace.tags`.
"""

from __future__ import annotations

import json
from typing import Any

from clinic_desk.llm.metered import Call
from clinic_desk.simulator.judge import Verdict
from clinic_desk.simulator.runner import Conversation, Turn
from clinic_desk.tracing import Span, Trace

OBS = "langfuse.observation."


def build_trace(c: Conversation, run_id: str, split: str = "") -> Trace:
    verdict = "code_pass" if c.code_pass else ("not_counted" if not c.counted else "code_fail")
    tags = tuple(t for t in (c.category, c.end, verdict, split) if t)
    trace = Trace(f"sim {c.persona_id}")
    first, last = _bounds(c)
    trace.root.start_ns, trace.root.end_ns = first, last
    trace.root.set_many(
        {
            "langfuse.session.id": run_id,
            "langfuse.trace.tags": tags,
            "langfuse.trace.metadata.persona": c.persona_id,
            "langfuse.trace.metadata.repeat": c.repeat,
            "langfuse.trace.metadata.end": c.end,
            "langfuse.trace.metadata.calendar": f"{c.calendar_ok} · {c.calendar_why}",
            "langfuse.trace.metadata.false_claims": c.false_claims,
            "langfuse.trace.metadata.claims_blocked": c.claims_blocked,
            "langfuse.trace.metadata.language_misses": c.language_misses,
            "langfuse.trace.metadata.usd": str(c.usd),
            OBS + "input": json.dumps(c.turns[0].patient if c.turns else ""),
            OBS + "output": json.dumps(f"end {c.end}; calendar {c.calendar_ok}: {c.calendar_why}"),
        }
    )
    if c.error:
        trace.root.error = c.error
    turns = [(t, _turn_span(trace, t)) for t in c.turns]

    def parent(start_ns: int) -> Span | None:
        return next((s for t, s in turns if t.start_ns <= start_ns <= t.end_ns), None)

    for call in c.calls:
        usage = {"input": call.input_tokens, "output": call.output_tokens}
        trace.add(
            call.role,
            parent(call.start_ns),
            call.start_ns,
            call.end_ns,
            {
                OBS + "type": "generation",
                OBS + "model.name": call.model,
                OBS + "input": json.dumps(call.input, ensure_ascii=False),
                OBS + "output": json.dumps(call.output, ensure_ascii=False),
                OBS + "usage_details": json.dumps(usage),
                OBS + "cost_details": json.dumps({"total": float(call.usd)}),
                OBS + "metadata.stop": call.stop,
                OBS + "metadata.provider": call.provider,
            },
        )
    for tool in c.tools:
        attrs: dict[str, Any] = {
            OBS + "type": "tool",
            OBS + "input": json.dumps(tool.arguments, ensure_ascii=False, default=str),
            OBS + "output": json.dumps(tool.result, ensure_ascii=False),
        }
        if tool.is_error:
            attrs[OBS + "level"] = "ERROR"
        trace.add(tool.name, parent(tool.start_ns), tool.start_ns, tool.end_ns, attrs)
    return trace


def _turn_span(trace: Trace, t: Turn) -> Span:
    flags = [
        *(["false claim"] if t.false_claim else []),
        *(["claim blocked"] if t.claim_blocked else []),
        *(["wrong language"] if not t.reply_lang_ok else []),
        *(["persona off-language"] if not t.persona_lang_ok else []),
    ]
    attrs: dict[str, Any] = {
        OBS + "type": "span",
        OBS + "input": json.dumps(t.patient, ensure_ascii=False),
        OBS + "output": json.dumps(t.desk, ensure_ascii=False),
    }
    if t.writes:
        attrs[OBS + "metadata.writes"] = json.dumps(t.writes)
    if t.lang_fix:
        attrs[OBS + "metadata.lang_fix"] = t.lang_fix
    if flags:
        attrs[OBS + "level"] = "WARNING"
        attrs[OBS + "status_message"] = ", ".join(flags)
    return trace.add(f"turn {t.n}", None, t.start_ns, t.end_ns, attrs)


def _bounds(c: Conversation) -> tuple[int, int]:
    times = [t.start_ns for t in c.turns] + [x.start_ns for x in c.calls]
    ends = [t.end_ns for t in c.turns] + [x.end_ns for x in c.calls]
    times, ends = [x for x in times if x], [x for x in ends if x]
    return (min(times), max(ends)) if times and ends else (0, 0)


def build_judge_trace(trace_id: str, verdict: Verdict, calls: list[Call]) -> Trace:
    """The judge's spans, added to a conversation's trace after the pass: one `judge` span with
    the verdict (failing criteria first, each with its evidence) and its model calls inside."""
    failed = [cv for cv in verdict.criteria if cv.applies and not cv.passed]
    trace = Trace("judge", trace_id=trace_id)
    if calls:
        trace.root.start_ns = min(c.start_ns for c in calls)
        trace.root.end_ns = max(c.end_ns for c in calls)
    else:
        trace.root.end_ns = trace.root.start_ns
    criteria = {
        cv.id: {"label": cv.label, "evidence": cv.evidence, "reason": cv.reason}
        for cv in sorted(verdict.criteria, key=lambda cv: cv.label != "no")
    }
    trace.root.set_many(
        {
            OBS + "input": json.dumps(verdict.conversation),
            OBS + "output": json.dumps(
                {"passed": verdict.passed, "failed": [cv.id for cv in failed], **criteria},
                ensure_ascii=False,
            ),
            OBS + "metadata.model": verdict.model,
            OBS + "metadata.lint": json.dumps(verdict.lint, ensure_ascii=False),
        }
    )
    if verdict.error:
        trace.root.error = verdict.error
    elif failed:
        trace.root.set_many({OBS + "level": "WARNING"})
    for call in calls:
        trace.add(
            "judge",
            trace.root,
            call.start_ns,
            call.end_ns,
            {
                OBS + "type": "generation",
                OBS + "model.name": call.model,
                OBS + "input": json.dumps(call.input, ensure_ascii=False),
                OBS + "output": json.dumps(call.output, ensure_ascii=False),
                OBS + "usage_details": json.dumps(
                    {"input": call.input_tokens, "output": call.output_tokens}
                ),
                OBS + "cost_details": json.dumps({"total": float(call.usd)}),
            },
        )
    return trace
