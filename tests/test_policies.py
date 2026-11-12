"""How policies reach the receptionist: all in the prompt (default) or a search tool (BM25 or
embeddings). Both modes stay runnable so the simulator can compare their answers. No network:
the dense retriever runs on a fake embedder."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Sequence
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from clinic_desk.clinic import load_clinic
from clinic_desk.llm.base import ToolResults
from clinic_desk.llm.scripted import ScriptedLLM, call, say
from clinic_desk.receptionist.graph import Receptionist, make_desk, policies_block
from clinic_desk.retrieval import BM25, Overlap, chunks_from, tokenize
from clinic_desk.retrieval.dense import Dense, cached, cosine
from clinic_desk.retrieval.evaluate import evaluate, load_questions

NOW = datetime(2026, 11, 11, 10, 0, tzinfo=ZoneInfo("Europe/Madrid"))
CANCEL_ES = "Las citas se pueden cancelar o cambiar sin coste hasta 24 horas antes."
CANCEL_CA = "Les cites es poden cancel·lar o canviar sense cost fins a 24 hores abans."


def tool_names(llm: ScriptedLLM) -> set[str]:
    return {t.name for t in llm.requests[0].tools}


def test_by_default_every_policy_is_in_the_prompt_in_both_languages() -> None:
    llm = ScriptedLLM([say("Hasta 24 horas antes, sin coste.")])
    asyncio.run(Receptionist(make_desk(llm, lambda: NOW)).reply("¿Puedo anular la cita?"))

    system = llm.requests[0].system
    assert CANCEL_ES in system and CANCEL_CA in system
    assert "search_policies" not in tool_names(llm)
    # the policies sit before the per-turn context, so the fixed part stays cacheable
    assert system.index("# Clinic policies") < system.index("# Context")


def test_the_fixed_prompt_does_not_change_with_the_language() -> None:
    llm = ScriptedLLM([say("Hola."), say("Bon dia.")])
    r = Receptionist(make_desk(llm, lambda: NOW))
    asyncio.run(r.reply("Hola, quiero una cita"))
    asyncio.run(r.reply("Bon dia, vull una cita"))
    first, second = (req.system.split("# Context")[0] for req in llm.requests)
    assert first == second


def test_search_policies_is_refused_in_prompt_mode() -> None:
    llm = ScriptedLLM([call("search_policies", query="cancelar"), say("Vale.")])
    asyncio.run(Receptionist(make_desk(llm, lambda: NOW)).reply("¿Puedo anular?"))
    results = llm.requests[1].messages[-1]
    assert isinstance(results, ToolResults) and results.results[0].is_error
    assert json.loads(results.results[0].content)["error"] == "unknown_tool"


def test_search_mode_offers_the_tool_and_answers_in_the_patient_language() -> None:
    # a Spanish query for a Catalan patient (seen in a real chat) still finds the policy
    llm = ScriptedLLM(
        [call("search_policies", query="cancelar cita penalización"), say("D'acord.")]
    )
    desk = make_desk(llm, lambda: NOW, policies="search")
    asyncio.run(Receptionist(desk, "ca").reply("Puc anul·lar la cita?"))

    assert "search_policies" in tool_names(llm)
    assert "# Clinic policies" not in llm.requests[0].system
    results = llm.requests[1].messages[-1]
    assert isinstance(results, ToolResults)
    top = json.loads(results.results[0].content)["results"][0]
    assert top["policy_id"] == "cancellation" and top["text"].startswith(CANCEL_CA)


def test_policies_block_lists_every_policy() -> None:
    clinic = load_clinic()
    block = policies_block(clinic)
    assert all(f"## {p.id}" in block for p in clinic.policies)


# --- retrievers ----------------------------------------------------------------------------


def test_tokenize_strips_accents_stopwords_and_stems() -> None:
    assert tokenize("La cancelación de la cita") == ["cance", "cita"]
    assert tokenize("cancel·lar") == ["cance"]
    assert tokenize("cancelación", stem=None) == ["cancelacion"]


def test_bm25_weighs_rare_words_over_common_ones() -> None:
    bm25 = BM25(chunks_from(load_clinic()))
    assert bm25.rank("silla de ruedas")[0] == "location"
    assert bm25.rank("menores de 16 años")[0] == "minors"


def test_dense_ranks_by_cosine_and_caches(tmp_path: Path) -> None:
    chunks = chunks_from(load_clinic())
    calls: list[int] = []

    async def fake_embed(texts: Sequence[str]) -> list[list[float]]:
        # a 2-d "meaning": about personal data, about wheelchairs; plus a small constant
        calls.append(len(texts))
        return [
            [float("datos" in t or "dades" in t), float("silla" in t or "cadira" in t), 0.1]
            for t in texts
        ]

    embed = cached(fake_embed, tmp_path / "vectors.json")

    async def run() -> list[str]:
        dense = await Dense.build("fake", chunks, embed)
        await dense.prepare(["mis datos"])
        return dense.rank("mis datos")

    assert asyncio.run(run())[0] == "privacy"
    first = sum(calls)
    asyncio.run(run())  # everything cached now
    assert sum(calls) == first
    assert cosine([1.0, 0.0], [0.0, 0.0]) == 0.0


def test_the_eval_set_is_valid_and_lexical_results_are_stable() -> None:
    clinic = load_clinic()
    questions = load_questions(clinic)
    assert len(questions) == 20
    assert {q.lang for q in questions} == {"es", "ca"}
    # pinned numbers (2026-11-12): a change here is a change to retrieval, report it
    chunks = chunks_from(clinic)
    assert sum(evaluate(Overlap(chunks), questions).hit1.values()) == 13
    assert sum(evaluate(BM25(chunks, stem=None), questions).hit1.values()) == 11
