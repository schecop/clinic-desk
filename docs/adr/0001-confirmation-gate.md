# ADR-0001: One agent node, writes only through a deterministic confirmation gate

- **Date:** 2026-11-11
- **Status:** accepted

## Context
Clinics will not let an AI touch their calendar unless they can trust it. The rule is "no book, move or cancel until the patient has confirmed the exact slot", and it must be enforced by the graph, not by the prompt: the coach will rewrite the prompt, and a prompt rule can be lost in one change. The plan sketched a linear graph (identify → intent → collect → confirm → act).

## Decision
- **One agent node** (LLM + tool loop) runs the conversation. Identifying the patient and collecting the details are guards inside the tools (`identify_patient_first`, `not_this_patients_appointment`), not nodes.
- **The model has no write tool.** `propose_booking` / `propose_move` / `propose_cancel` validate the write with the scheduler's dry run and save it as `pending` in the state.
- **`ask_confirm` renders the question from a template**, built from the pending slot in the patient's language. It ends the turn.
- **`confirm` classifies the next reply with rules** (ES/CA). Only a short, plain yes counts as yes. A no drops the pending action. *Amended 2026-11-11:* an unclear reply (a question, "sí, pero…") keeps it. The agent answers, a new `propose_*` may replace it, and the graph repeats the template; 3 unclear replies in a row drop it. The first version dropped it on any unclear reply, and in a real chat the model then asked "¿confirma?" itself and claimed a move that never happened (`FAILURES.md`).
- **`act` is the only node that writes**, it is reachable only from `confirm`, and the scheduler re-checks the slot under a lock.

## Alternatives considered
- **Linear chain of nodes (the plan's sketch):** real calls do not follow a fixed order. Patients ask a price mid-booking, change their mind, or give everything in one sentence. It also costs one LLM call per node.
- **A write tool the model calls after the patient says yes:** the model would decide what counts as a yes, so the rule would live in the prompt.
- **An LLM classifier for the yes/no:** it costs money and latency on every confirmation, and it is less predictable on the write path. It stays the fallback if the simulator shows too many missed yeses.
- **The model writes the confirmation question:** it could paraphrase the slot ("el lunes por la mañana"), so the patient would not confirm the exact slot.

## Consequences
- The guarantee is testable with a scripted model: a model that lies ("¡Reservado!"), calls a non-existent `book_appointment`, or meets a "sí, pero…" writes nothing (`tests/test_receptionist.py`). A mutation that turns every reply into a yes fails 3 tests.
- Cost of being safe: every write takes at least one extra turn, and a missed yes costs one more question. Measure the missed-yes rate with the simulator (D3), and revisit the rules or add an LLM fallback if it is high.
- The confirmation template is fixed text in Spanish and Catalan. The coach cannot improve it, because it only edits `prompts/`.
- Language detection uses marker words. A one-word reply keeps the current language.
