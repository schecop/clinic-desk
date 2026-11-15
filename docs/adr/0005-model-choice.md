# ADR-0005: Model per role

- **Date:** 2026-11-15
- **Status:** accepted (receptionist: provisional, see below)

## Context
No default model (CLAUDE.md): each role gets the cheapest model that passes its own test, compared on quality, latency and cost. Shortlists, prices and kill criteria are in `docs/model-research.md`. Budget: $15 for the whole week, all paid tiers (free tiers failed or were too slow). This ADR records what was chosen and **how much of it was measured**.

## Decision

| Role | Model | Price in / out ($/Mtok) | Evidence | Measured? |
|---|---|---|---|---|
| Receptionist | **Claude Haiku 5.5**, thinking off, effort low | 0.10 / 0.50 | Verify pass `20261115-144158-verify` (28 conversations): $0.008 per conversation incl. persona and translator; latency per turn p50 1.4 s / p95 3.1 s (target p95 ≤ 2 s: **missed**); calib3 gave the same (1.4 / 3.2 s) | Alone. No comparison run |
| Persona | **Mistral Small** (`mistral-small-latest`) | 0.15 / 0.60 (list price unclear; the higher one) | Gemma 4 (free): HTTP 500 on a whole pass. Ministral 3 8B: drifted from the cards (code pass 8/28, mostly its fault). Mistral Small: 4/4 faithful on the personas Ministral broke; $0.006 per conversation | Yes, 3 models |
| Judge | **Gemini 3.8 Flash**, reasoning low, + code lint | 0.75 / 3.75 (intro price to 2026-12-31) | 20 hand-labelled conversations (18 ca): 92% agreement per criterion; 97% same labels between two runs. GPT-5.4 mini: 84% alone (92% with the lint), moves up to 15 points between runs | Yes, 2 models (+ reasoning/format variants) |
| Coach | **Claude Sonnet 5.5**, effort medium | 2.00 / 10.00 | First proposal: $0.018, a valid single change on the first try. Outcome: `FAILURES.md` / coach change log | Alone. Opus comparison not run |
| Embeddings | **none by default** (all policies in the cached prompt, ADR-0003); text-embedding-3-small when `policies: search` | 0.02 | hit@1 17/20 vs BM25 11/20, word overlap 13/20 | Yes, 3 methods |

## Alternatives considered
- **Receptionist, Mistral Small 4 / Gemini 3.5 Flash-Lite / GPT-5.4 mini:** not run. The comparison (~$0.45 per model for a visible pass) was cut for time: the week's hours went to the simulator, judge calibration, gate and coach, which every later comparison depends on. Now that the gate exists, a comparison is one `simulate` + `gate` per model.
- **Judge, Claude Sonnet 5.5:** same family as the receptionist (self-preference risk) and ~2.5× the price; Gemini reached the target without it.
- **Coach, Claude Opus 5.5:** one-off comparison planned on a fixed failure batch; not run (time).

## Consequences
- The receptionist choice is the weakest one: Haiku is "good enough to measure", not "best of three". The p95 latency target is missed (3.1 s). Revisit before any voice work: run Mistral Small 4 and Gemini 3.5 Flash-Lite on the visible split with the best coach prompt, compare with the gate's per-criterion table plus p95.
- The judge's 92% is optimistic (rubric and lint tuned on the same 20). A fresh held-out label check is still open.
- Gemini 3.8 Flash doubles in price on 2026-12-31: re-check the judge cost per pass then.
- Product cost metric, measured: **~$0.016 per judged conversation**, **~$0.45 per visible pass** (14 × 2), **~$0.02 per coach proposal**.
