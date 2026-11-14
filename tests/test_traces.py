"""The Langfuse trace of a simulated conversation: structure, attributes, and the export. No
network: the exporter posts to an httpx MockTransport."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import httpx

from clinic_desk.clinic import load_clinic
from clinic_desk.llm.scripted import ScriptedLLM, call
from clinic_desk.simulator.persona import load_personas
from clinic_desk.simulator.runner import RoleModel, run_conversation
from clinic_desk.simulator.simulate import run_pass
from clinic_desk.simulator.traces import build_trace
from clinic_desk.tracing import LangfuseExporter
from tests.test_simulator import CARMEN_DESK, CARMEN_PATIENT


def carmen() -> Any:
    persona = next(p for p in load_personas(load_clinic()) if p.id == "carmen-move-afternoon")
    return asyncio.run(
        run_conversation(
            persona,
            receptionist=RoleModel(ScriptedLLM(CARMEN_DESK)),
            persona_model=RoleModel(ScriptedLLM(CARMEN_PATIENT)),
        )
    )


def attrs(span: dict[str, Any]) -> dict[str, Any]:
    out = {}
    for kv in span["attributes"]:
        v = kv["value"]
        out[kv["key"]] = (
            [x["stringValue"] for x in v["arrayValue"]["values"]]
            if "arrayValue" in v
            else next(iter(v.values()))
        )
    return out


def test_a_conversation_becomes_one_trace_with_turns_generations_and_tools() -> None:
    c = carmen()
    spans = build_trace(c, "run-1", "visible").to_otlp()["resourceSpans"][0]["scopeSpans"][0][
        "spans"
    ]
    by_name: dict[str, list[dict[str, Any]]] = {}
    for s in spans:
        by_name.setdefault(s["name"], []).append(s)
    root = by_name["sim carmen-move-afternoon"][0]
    assert "parentSpanId" not in root
    a = attrs(root)
    assert a["langfuse.session.id"] == "run-1"
    assert a["langfuse.trace.tags"] == ["move", "done", "code_pass", "visible"]

    turns = {s["spanId"]: s for name, ss in by_name.items() if name.startswith("turn") for s in ss}
    assert len(turns) == len(c.turns)
    # every generation and tool sits inside a turn, and is typed for Langfuse
    gens = by_name["receptionist"] + by_name["persona"]
    assert len(gens) == len(c.calls)
    for g in gens:
        assert g["parentSpanId"] in turns
        ga = attrs(g)
        assert ga["langfuse.observation.type"] == "generation"
        assert json.loads(ga["langfuse.observation.usage_details"]).keys() == {"input", "output"}
    tool = by_name["find_slots"][0]
    assert tool["parentSpanId"] in turns
    assert attrs(tool)["langfuse.observation.type"] == "tool"
    assert int(tool["startTimeUnixNano"]) <= int(tool["endTimeUnixNano"])
    # the turn that wrote says so
    writes = [attrs(s).get("langfuse.observation.metadata.writes") for s in turns.values()]
    assert any(w and "moved A003" in w for w in writes)


def test_the_pass_exports_each_conversation_and_keeps_the_trace_id(tmp_path: Path) -> None:
    posted: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == LangfuseExporter.PATH
        assert request.headers["x-langfuse-ingestion-version"] == "4"
        posted.append(json.loads(request.content))
        return httpx.Response(200, json={})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="http://lf")
    exporter = LangfuseExporter("http://lf", "pk", "sk", client=client)
    persona = next(p for p in load_personas(load_clinic()) if p.id == "carmen-move-afternoon")
    _, conversations = asyncio.run(
        run_pass(
            [persona],
            receptionist=RoleModel(ScriptedLLM(CARMEN_DESK)),
            persona_model=RoleModel(ScriptedLLM(CARMEN_PATIENT)),
            run_id="t",
            out=tmp_path,
            ledger=tmp_path / "cost.jsonl",
            exporter=exporter,
        )
    )
    assert len(posted) == 1
    trace_id = posted[0]["resourceSpans"][0]["scopeSpans"][0]["spans"][0]["traceId"]
    assert conversations[0].trace_id == trace_id


def test_a_failed_export_does_not_fail_the_pass(tmp_path: Path) -> None:
    client = httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _: httpx.Response(500)), base_url="http://lf"
    )
    exporter = LangfuseExporter("http://lf", "pk", "sk", client=client)
    persona = next(p for p in load_personas(load_clinic()) if p.id == "carmen-move-afternoon")
    _, conversations = asyncio.run(
        run_pass(
            [persona],
            receptionist=RoleModel(ScriptedLLM(CARMEN_DESK)),
            persona_model=RoleModel(ScriptedLLM(CARMEN_PATIENT)),
            run_id="t",
            out=tmp_path,
            ledger=tmp_path / "cost.jsonl",
            exporter=exporter,
        )
    )
    assert conversations[0].trace_id is None and conversations[0].code_pass


def test_the_verdict_joins_the_conversation_trace(tmp_path: Path) -> None:
    from clinic_desk.simulator.evaluate import judge_pass
    from tests.test_judge import CARMEN, CLINIC, PERSONAS, entries

    posted: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        posted.append(json.loads(request.content))
        return httpx.Response(200, json={})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="http://lf")
    exporter = LangfuseExporter("http://lf", "pk", "sk", client=client)
    c = carmen().model_copy(update={"trace_id": "ab" * 16})
    llm = ScriptedLLM([call("record_verdict", criteria=entries(fail="tone"))])
    asyncio.run(
        judge_pass(
            [c],
            llm,
            run_id="r",
            folder=tmp_path,
            role="judge",
            personas=PERSONAS,
            clinic=CLINIC,
            exporter=exporter,
        )
    )
    [body] = posted
    spans = body["resourceSpans"][0]["scopeSpans"][0]["spans"]
    assert {s["traceId"] for s in spans} == {c.trace_id}  # the same trace, not a new one
    root = next(s for s in spans if "parentSpanId" not in s)
    a = attrs(root)
    assert "langfuse.trace.name" not in a  # the conversation keeps its own name
    assert json.loads(a["langfuse.observation.output"])["failed"] == ["tone"]
    [gen] = [s for s in spans if s.get("parentSpanId") == root["spanId"]]
    assert attrs(gen)["langfuse.observation.type"] == "generation"
    assert CARMEN.id in json.loads(a["langfuse.observation.input"])
