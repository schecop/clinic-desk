# ADR-0003: Policies in the prompt by default; retrieval kept as an evaluated option

- **Date:** 2026-11-12
- **Status:** accepted

## Context
Policy questions must be answered from `data/clinic/policies.yaml`, never from the model's memory. The model can get that text two ways: search for it (a `search_policies` tool, light RAG) or have every policy in its prompt. We measured the search step on 20 colloquial questions (10 ES, 10 CA, written as a patient would say them) with `clinic-desk eval-retrieval`:

| retriever | hit@1 | hit@3 |
|---|---|---|
| word overlap (D2 placeholder) | 13/20 | 13/20 |
| BM25 | 11/20 | 14/20 |
| BM25 + 5-letter stem | 9/20 | 13/20 |
| text-embedding-3-small | 17/20 | 19/20 |

Lexical retrieval fails on vocabulary mismatch ("Tengo Sanitas" vs "aseguradora", "coche" vs "aparcamiento"): when no word is shared every policy scores 0. Embeddings win by 4 at hit@1, so by the rule fixed in `docs/model-research.md` BM25 loses. BGE-M3 was not run (torch, ~2 GB, for 20 chunks). This measures the worst case: in a call the model writes the query and may rephrase it.

## Decision
- **Default `receptionist.policies: prompt`:** every policy, both languages (~1.5k tokens), goes into the fixed part of the system prompt, before the per-turn context, so it stays cacheable and a language switch does not change it. Built by code from the data (`policies_block`), not in `prompts/`: the coach cannot edit facts. `search_policies` is not offered.
- **Option `receptionist.policies: search`** keeps the tool, with `retriever: bm25` (local) or an OpenAI embedding model. Both modes run through the same graph, and the simulator compares their final answers (D3). This ADR is revisited with those numbers.
- The rule "Facts come from tools" becomes "Facts come from the clinic's data, never from the model" (CLAUDE.md).

## Alternatives considered
- **Embeddings in the tool as the default:** 3/20 misses in the worst case, one more network call per question (latency is already over the 2 s p95 target), and the patient's question leaves the machine.
- **BM25 in the tool:** free and local, but 11/20.
- **Hybrid BM25 + embeddings:** more moving parts for a 20-chunk corpus.

## Consequences
- No retrieval misses by construction, but the model reads 10 policies every turn and may mix them up. Retrieval accuracy is not answer accuracy; only the judge measures the latter.
- Scales to a few dozen policies. With a much larger corpus, re-run `eval-retrieval` and switch the default.
- Prompt caching only applies above a minimum prompt length per model: to be checked when cost is measured (D3).
