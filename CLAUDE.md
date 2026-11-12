# Clinic Desk — instructions for Claude Code

## What this is
A bilingual (Spanish/Catalan) AI receptionist for small private clinics (dentists, physios). It books, moves and cancels appointments and answers questions about the clinic's policies. Clinics lose patients when nobody picks up the phone, but they will not let an AI touch their calendar unless they can trust it. So the receptionist ships with a **patient simulator** (LLM-played personas: an elderly patient, an angry one, vague dates, switches to Catalan mid-call, asks for medical advice it must refuse), an **LLM judge** with a rubric, and a **coach**: an agent that reads failed conversations, proposes one prompt change at a time, re-runs the simulator and keeps or rolls back the change on its own.

A one-week learning build (NNN week 2, 9-15 Nov 2026, ~20h). **Lead skill:** evals. **Also practised:** LangGraph (2nd pass), APIs + OAuth (Google Calendar), a self-improving agent loop (the coach, 1st pass), observability (2nd), debugging from traces (2nd), light RAG.

**Done when:** a deliberately bad prompt change fails the regression gate with a report of which personas broke and why; **and** the coach, starting from a weak prompt, improves the score on the held-out personas, with every change explained in its change log.

Key files: `FAILURES.md` (real failures and fixes), `docs/model-research.md` (model choice), `docs/adr/` (lasting decisions), `config/example.yaml` (tunables and the model per role), `data/clinic/` (the synthetic clinic).

Operator context (plan, progress, teach-back) lives in `private/`, a separate private repo that is gitignored here:

@private/CLAUDE.md

## Architecture rules (non-negotiable)
- **No write without confirmation.** The receptionist never books, moves or cancels until the patient has confirmed the exact slot; the graph enforces it with a confirmation node, not the prompt.
- **No medical advice.** Symptoms or treatment questions are declined and redirected (call the clinic, or 112 for emergencies). The judge scores this on every conversation.
- **Facts come from the clinic's data, never from the model.** Opening hours, prices and free slots come from tools. Policies are loaded into the fixed prompt from `data/clinic/policies.yaml` by code (or, in `search` mode, from the `search_policies` tool); the coach cannot edit them (ADR-0003).
- **The calendar is a port.** Simulations always run against the in-memory fake calendar; Google Calendar is one adapter, used for the demo and the OAuth pass.
- **The coach never sees the held-out personas**, and a change is kept only by the keep rule fixed beforehand (N repeats; the gain must beat the spread between passes; no regression on held-out). Every change is logged: prompt diff, reason, scores before and after, kept or rolled back.
- **Synthetic data only.** No real patient data anywhere: code, fixtures, traces, prompts.

## How we work: fast mode, demo first
Pablo learns by seeing things work, then reasoning about why, then rebuilding with more understanding. Speed matters, but nothing should stay a black box.

1. **Claude builds most of the code; Pablo directs, runs, breaks and fixes.** No tutor mode, no guided exercises.
2. **Demo first, then why.** When a piece works, show it running (a command, a test, an output). Then explain in 5-10 lines how it works and *why it's built this way*: the main alternative and why not. Point to the 2-3 lines that matter most.
3. **Small vertical slices.** Each step ends in something runnable. Prefer a rough end-to-end path over a polished half.
4. **Ask before big forks.** For library, provider, data model or architecture choices, give a recommendation with one line of trade-offs and wait for a yes. Everything else: proceed.
5. **Must before stretch.** Track the must scope, and propose cutting stretch items when the budget is at risk.

## No default model
Latency and cost matter as much as quality. Any model is allowed: open-source, free, cheap or frontier.
- **Research first:** `docs/model-research.md` lists the candidates per role (receptionist, persona, judge, coach, embeddings; STT/TTS only if the voice stretch happens) with price in/out, latency, context, tool-use quality, Spanish/Catalan quality and hosting. Search for current data; never answer from memory.
- **Model-agnostic code:** everything goes through `clinic_desk.llm.LLMClient`. The model per role lives in `config/*.yaml` (`llm:`), never in code. Provider adapters normalise tool-use formats. LangGraph nodes call `LLMClient`; no LangChain chat-model wrappers.
- **Organise for cost:** no LLM where deterministic code works (slot search, date handling where possible); small model by default, stronger only where evals show it pays off; personas on the cheapest model that stays in character; the judge calibrated on ~20 hand-labelled conversations before it is chosen, run in batch where possible; prompt caching for the long fixed prompts.
- **Decide with evals:** compare 2-3 models per role on quality, latency p50/p95 and cost; record the choice in an ADR `model-choice`.
- **Product cost metric:** cost per conversation and per full simulator pass.

## Records (keep them as you go, not at the end)
- **`FAILURES.md`:** every real failure: symptom, how it was found (trace, judge, test), root cause class (prompt, tool, retrieval, model, simulator, judge), fix, regression test or eval case added.
- **`docs/adr/`:** one short ADR per lasting decision (copy `0000-template.md`).

## Conventions
- Python 3.12+, uv, ruff, mypy (strict), pytest, `src/` layout.
- LLM prompts live in `prompts/`. Templates that are part of the confirmation gate (confirmation question, handoff, context block) stay in code. The coach edits only `prompts/receptionist.md`.
- Observability from day 1: every simulated conversation is one Langfuse trace (receptionist, persona and judge calls as generations, tool calls as spans), and the eval report links to it.
- Tests never call live models or APIs: a scripted fake `LLMClient` (fixed replies and tool calls) drives the real graph and loops, and runs in CI. Real-model evals are separate, opt-in, and run on purpose.
- Never commit secrets. Keys go in `.env` (gitignored) and are listed in `.env.example`; the Google OAuth token lives in `token.json` (gitignored).
- Never commit on your own: show `git status`, propose a conventional commit (`feat:`, `fix:`, `test:`, `docs:`, `chore:`) with a why-body, and commit on Pablo's go-ahead.

## Safety
- Google Calendar: a dedicated test calendar only, never Pablo's personal one. Scopes limited to calendar events.
- Claude never runs a simulator pass or an eval sweep against a paid API without stating the expected cost first; free tiers and the fake model need no confirmation.
