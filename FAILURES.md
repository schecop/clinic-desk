# Failures

Every real failure: symptom, how it was found (trace, judge, test), root cause class (prompt, tool, retrieval, model, simulator, judge), fix, and the regression test or eval case added. Newest first.

<!-- Template:
## YYYY-MM-DD · short title
- **Symptom:**
- **Found by:**
- **Root cause (class):**
- **Fix:**
- **Regression guard:**
-->

## 2026-11-12 · The simulator blamed the receptionist for its own bug
- **Symptom:** first real pass (3 personas, Gemma 4 as patient, Haiku as receptionist): code pass 1/3. Carmen's move and Marc's first visit were never written, so `calendar: False`.
- **Found by:** reading the transcripts before trusting the score. Both patients answered the confirmation with "Sí, estoy de acuerdo. Muchas gracias, adiós. [DONE]" in one turn.
- **Root cause (class):** simulator. The runner ended the call on `[DONE]` without passing that line to the receptionist, so the yes that the write depends on never arrived. The receptionist did nothing wrong: the score measured the simulator.
- **Fix:** a line carrying an end marker is still delivered, and the call ends after the receptionist's reply. Persona prompt: answer a confirmation first, do not end the call in the same turn.
- **Regression guard:** `test_a_yes_said_together_with_the_end_marker_still_writes`. Re-run: code pass 2/2 counted.

## 2026-11-12 · Gemma 4 thinks before every line, and sometimes never speaks
- **Symptom:** the first pass crashed on `ReadTimeout` (4 attempts). In a probe, 56 s and an empty answer with `max_tokens` 200. Later, one empty patient turn after 32 s (`stop=max_tokens`).
- **Found by:** a timed one-call probe, then the `calls` log of the conversation JSON.
- **Root cause (class):** simulator (model). Gemma 4 on the Gemini API always writes `<thought>…</thought>` before the answer and rejects `reasoning_effort` ("Thinking budget is not supported", HTTP 400). Asking it not to think in the prompt does not work. Also a runner bug: one failing persona call stopped the whole pass.
- **Fix:** the OpenAI-compatible adapter strips the thought block from the text and from the replayed turn; persona `max_tokens` 1500; an empty line is retried once; a persona or receptionist API error ends that conversation as `error` (out of the score), not the pass. Persona model 26B-A4B (~7 s a call) instead of 31B (~30 s).
- **Regression guard:** `tests/test_llm_adapters.py` (split, raw replay, failure isolation), `test_an_empty_persona_line_is_retried_once`.

## 2026-11-12 · "la seva cita de extracció"
- **Symptom:** the Catalan confirmation template read "cita de extracció simple".
- **Found by:** first real pass transcript.
- **Root cause (class):** tool (template). No elision of "de" before a vowel in Catalan.
- **Fix:** `language.of()` gives "d'extracció", "d'higiene", "de revisió".
- **Regression guard:** `test_catalan_elides_de_before_a_vowel`.

## 2026-11-11 · The receptionist claimed a move that never happened
- **Symptom:** at the confirmation question Carmen asked "¿Y eso cuánto me cuesta?". The receptionist gave the price, then asked "¿Confirma el cambio…? Responda sí o no" itself, twice. On "Sí" it said "Ya está confirmado… queda cambiada para el lunes 16 a las 16:00". The calendar still had the Wednesday 09:30 appointment. The gate held (no write without a yes to the template), but the patient was told a falsehood about her appointment: the worst kind of failure for this product.
- **Found by:** second real-model chat with `--trace` (Pablo): after the question, `pending None`; no `propose_move` call on the later turns; the context block said "did NOT confirm… nothing was written" while the model said it was done.
- **Root cause (class):** graph design, then model. An `unclear` reply dropped the proposal (ADR-0001 as first written), which left re-proposing up to the model. Haiku 5.5 imitated the confirmation question in its own words instead, and then claimed success despite the context.
- **Fix:** an `unclear` reply keeps the proposal. The agent answers, then `ask_confirm` repeats the template. A new `propose_*` may replace it, and 3 unclear replies in a row drop it. The context block shows the pending proposal as "NOT confirmed and NOT written". Prompt: a yes only counts as an answer to the system's question; if nothing is pending, propose again.
- **Regression guard:** `test_a_question_at_confirmation_keeps_the_proposal_and_the_graph_asks_again`, `test_an_unclear_reply_can_replace_the_proposal`, `test_three_unclear_replies_in_a_row_drop_the_proposal`. Mutation (drop on unclear again) fails 2 of them. Still to add, by code in the simulator (D3): "claims a write that is not in the calendar diff", the hard check on every conversation.

## 2026-11-11 · Preambles spoken as part of the reply
- **Symptom:** "Voy a buscar su ficha con ese número." followed by "Hola, Carmen, ya la tengo identificada…": two greetings in one turn; also "No tengo el precio a mano, así que voy a consultarlo."
- **Found by:** second real-model chat.
- **Root cause (class):** graph. Text sent next to a tool call was added to the spoken reply.
- **Fix:** only final replies (no tool calls) and graph templates are spoken; preambles stay in the history only.
- **Regression guard:** `test_text_next_to_a_tool_call_is_not_spoken`.

## 2026-11-11 · "No afternoon slots" when there were 16
- **Symptom:** Carmen asked to move her checkup to Monday afternoon. The receptionist said Dr. Roca only had morning slots (09:00-10:15) on Monday 16. He had 36 free slots that day, 16 of them from 16:00.
- **Found by:** first real-model chat (Pablo, Haiku 5.5, `clinic-desk chat`), Carmen case.
- **Root cause (class):** tool. `find_slots` returned the earliest 6 slots with no sign that more existed, and had no way to ask for a time of day. The model read a truncated list as the whole day. The model's reasoning was right given that list: the false fact came from the tool's output.
- **Fix:** `find_slots` takes `time_from`/`time_to` (applied before the usual-dentist preference), returns `total_free`, and when it truncates it says "Showing the earliest 6 of N… never say a time is unavailable without searching it".
- **Regression guard:** `test_find_slots_says_when_it_truncates_and_can_search_afternoons`, `test_the_time_window_applies_before_the_usual_dentist_preference`. Simulator persona to add (D3): a patient who wants "por la tarde" on a day with free mornings.

## 2026-11-11 · Catalan question answered in Spanish
- **Symptom:** after 6 turns in Spanish, "I si al final no puc venir, què passa?" got a correct answer (24 h, 20 €), but in Spanish. The next Catalan turn was answered in Catalan.
- **Found by:** first real-model chat (same as above). `guess_lang` detected `ca` correctly, so the context block said Catalan.
- **Root cause (class):** prompt / model. The language rule was a middle line of the context block, and the earlier Spanish turns and Spanish tool results pulled the model the other way. One sample only, so the size of the problem is unknown.
- **Fix:** the language line is now the last line of the context block (the last thing the model reads), phrased "write your whole reply in Catalan, even if earlier turns, tool results or policies are in another language". **Did not work:** the second chat failed the same way (2 of 2 on the switch turn; the model also wrote its `search_policies` query in Spanish). A prompt line is not enough here. **Second fix (2026-11-12, ADR-0002):** code checks the reply's language; if it is clearly the other one, a second call translates it, kept only if the numbers match and the language is right, else the original is spoken. The marker lists counted words both languages use ("el", "es", "una", "del") as Spanish; removed. Not yet verified with the real model.
- **Regression guard:** `test_the_context_block_gives_today_and_named_days` checks the line is last; `tests/test_language_guard.py` (translated, discarded on a changed number or wrong language, no extra call when right). Mutations (guard off, number check off) fail 5 and 1 tests. Code-checked language criterion in the simulator (D3), with a mid-call ES→CA switch persona.
- **Also seen in this chat (for the judge's rubric, not fixed):** a Spanish leak in Catalan ("primer hueco del día": it reused a Spanish `clinic_info` result from an earlier turn); a Catalan gender error ("una queixal"); for medicines it pointed to "farmacèutic o metge" (rubric decision 2026-11-12, Pablo: a pass, as long as no advice comes before it and it also offers the clinic, or 112 if urgent); a markdown bullet list in a phone reply.

## 2026-11-11 · The double-booking test passed without the lock
- **Symptom:** `test_double_booking_is_impossible_even_when_concurrent` stayed green after removing the scheduler's `asyncio.Lock`, so it proved nothing.
- **Found by:** mutation check by hand (remove the lock, re-run the test).
- **Root cause (class):** tool (test double). The in-memory store never suspended at an `await`, so two concurrent bookings ran one after the other. A first fix (`sleep(0)` before the read) still passed: the second booking read the list after the first had written.
- **Fix:** the fake reads first and yields after (`await asyncio.sleep(0)` before returning), like real I/O. Without the lock both bookings now succeed and the test fails.
- **Regression guard:** the same test, now verified to fail without the lock.

## 2026-11-11 · Two services off the booking grid (D1)
- **Symptom:** two 20-min services on a 15-min slot grid: unbookable or overlapping slots.
- **Found by:** the clinic loader's cross-checks, on their first run.
- **Root cause (class):** fixture data.
- **Fix:** both set to 15 min.
- **Regression guard:** `Clinic._cross_checks` rejects durations and buffers off the grid.
