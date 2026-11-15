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

## 2026-11-15 · Spanish-speaking patients answer in Catalan from the first turn
- **Symptom:** in `verify` and `adopted`, 49 and 37 of ~200 patient turns are not in the card's language. Always the same personas, all `language: { start: es }`: carmen-move-afternoon, diagnosis-demand, new-patient-checkup, pedro-vague-cleaning, root-canal-installments, saturday-cleaning. Pedro's card is Spanish; his first line is "Bon dia, miri, vull demanar una neteja".
- **Found by:** checking the language guard for the D2 exit check (Pablo asked why the row was still open): the guard's 5 translations were all on Spanish-card personas speaking Catalan. `persona_lang_ok` is computed on every turn but only shown as a trace tag, never in the summary, report or gate, so it went unnoticed since D3.
- **Root cause (class):** simulator. The receptionist greets in Catalan ("Clínica Dental Arrel, bon dia.") and the persona model (Mistral Small) mirrors it; the card's language rule is not strong enough.
- **Effect:** the evaluation is mostly Catalan. Spanish is barely tested, the language-switch personas lose their point, and the `language` scores (4% → 22%) are mostly Catalan-lint scores. Gate comparisons stay fair (both sides use the same simulator), but absolute scores describe Catalan calls. It probably explains why the calibration sample was 18 of 20 Catalan.
- **Fix:** (Pablo) the receptionist greets in Spain Spanish ("Clínica Dental Arrel, buenos días.") and starts in Spanish instead of in the card's language, which it used to be given (`DEFAULT_LANG`), then follows the patient. The persona's card gives its *usual* language to start in, and the patient is free after that, as real people are: a first version that re-asked off-card lines and dropped the conversation was rejected (it enforced obedience, not realism). The judge's `language` criterion follows what the patient actually said. Each pass now reports the languages spoken and the personas off their card, so the coverage is visible.
- **Regression guard:** `tests/test_simulator.py` (Spanish greeting, the desk starts in Spanish without the card, an off-card line is recorded and not retried, the summary's coverage line). All earlier passes used the Catalan greeting: compare new passes only with new baselines.

## 2026-11-15 · "Hi ha res més?" after every answer, and while a confirmation is pending
- **Symptom:** after adopting the coach's tone rule ("After finishing a task, always ask whether they need anything else"), `format` fell 68% → 59% (visible) and 69% → 53% (held-out, pooled). Of the failures in `adopted` and `ho4-best`, 5 are new and come from that rule: (a) the closing question at the end of **every** answer in question-only calls (root-canal-installments r0 and r1: 4 turns in a row; insurance-sanitas r3); (b) the closing question **while a confirmation is pending** (swelling-112 r1 turn 6: "La cita queda pendent de confirmació… Hi ha res més?", then the system's confirmation question again; ibuprofen-then-emergency r2).
- **Found by:** judge (`format`, "repeats the same closing line"), read by category on Pablo's request; the gate did not flag it (z 0.7 and 1.3: the drop is small against the noise).
- **Root cause (class):** prompt (the coach's change). "After finishing a task" is read as "after every reply"; the model also treats a proposal as a finished task. In swelling-112 r1 a "Sí, sí, però que no…" was classified unclear (kept on purpose, 2026-11-14), so the confirmation was still pending when the model closed.
- **Unchanged:** lists (numbered or bulleted slots) are about half the format failures before and after the coach (visible 5 → 6, held-out 7 → 6): an older, separate problem; the coach's attempt at it (iteration 2) was rolled back.
- **Fix:** open (Pablo): prompt wording ("once, when the request is complete, never while a confirmation is pending") and/or a code guard in the confirmation step.
- **Regression guard:** none yet.

## 2026-11-15 · The coach's gain on visible did not show on held-out
- **Symptom:** first coach run (`runs/coach/20261115-152127-coach`, 5 iterations, 2 kept). Visible, start → best: all criteria 65% → 77% (z 2.6). Held-out: 69% → 71% (z 0.2). Per criterion on held-out: tone 25% → 100% (z 3.8) transferred; language 25% → 8% did not (it was 42% after the first change, so it swings); facts_right 100% → 75% (z 1.9, just under the gate).
- **Found by:** `clinic-desk gate` between the held-out passes of the start and best prompts, after the run.
- **Root cause (class):** eval design + prompt. (1) Held-out is 6 personas × 2 = 12 conversations: noise per criterion is ~15-20 points, too wide to show a gain smaller than the tone one. (2) Of the 3 new facts_right failures, one copies the coach's own example ("recordi arribar 10 minuts abans per omplir la fitxa i portar el DNI") onto a checkup, a first-visit rule; the same error was in the start's held-out pass too, where the judge passed it. Two call the hygienist "doctora Laia Soler": absent in the start pass, cause not established.
- **Fix (measurement):** start and best re-measured on held-out with 4 repeats ($0.64; `ho4-start`, `ho4-best`), pooled with the first measurement (≈32-34 conversations per prompt). Tone **replicated**: 28% → 91% (z 5.2). Total 69% → 75% (z 1.3): not proven. Language 19% → 21%: the visible gain did not transfer. facts_right 97% → 91% (z 1.0): the first drop was mostly noise (100% in the new sample). Format 69% → 53% (z 1.3): not significant, worth watching. No hard-line failure. The "doctora" error is still unexplained.
- **Regression guard:** the gate on held-out, now with 4 repeats when a result has to be trusted.

## 2026-11-15 · Simulated patient rate-limited, conversations lost
- **Symptom:** two held-out passes back to back (concurrency 4): 6 of 48 conversations ended in `error`, Mistral HTTP 429 "Rate limit exceeded" after 4 attempts, mostly `joan-switch-checkup`.
- **Found by:** the counted conversations in the gate (20/22 of 24) and `end == error` in the run.
- **Root cause (class):** simulator (provider rate limit; retries too short for Mistral's window).
- **Fix:** none yet; errors are excluded from the score, so results stay valid but lose data unevenly (one persona lost 3 of 4). Options: concurrency 2 for back-to-back passes, or a longer backoff on 429.
- **Regression guard:** none.

## 2026-11-15 · A per-persona regression gate that fails on noise
- **Symptom:** the first gate rule (a persona breaks if its mean criteria rate drops by more than max(10 points, its baseline spread), or it loses a code pass) failed 6 of 14 personas comparing calib3 with verify: two passes with a near-identical prompt and the same overall score (65% → 65%).
- **Found by:** running the gate on two real passes before trusting it on the bad prompt.
- **Root cause (class):** eval design. With 2 repeats, one flipped conversation moves a persona by 50 points, and the baseline spread is a 2-sample estimate of the noise. The rule had no statistical power per persona.
- **Fix:** pooled per criterion (Pablo): each criterion's pass rate over the whole pass, a two-proportion z-test, break at z ≥ 2; code checks, false claims and all criteria together as signals too. The personas named are the ones where a broken signal failed more often. Same pair: largest z 1.4, PASS. Trade-off: a break confined to one persona can hide in the pool.
- **Regression guard:** `tests/test_gate.py` (one flip is noise, 4 of 20 medical advice breaks, small drops everywhere break the whole pass, a baseline failure is not new); 6 mutations of the rule each fail a test.
- **Second failure, same day:** on the deliberately bad prompt ("suggest ibuprofen", numbered lists) the gate failed on `format` (z 3.8), but the medical hard line did not break: diagnosis-demand advised paracetamol in 2 of 2 conversations, yet the criterion applied in only 4, so z was 1.4. A pooled z-test cannot protect a rare criterion. **Fix (Pablo, post-hoc):** a hard line also breaks with 2+ failures and a higher failure rate than the baseline (ADR-0004). It was set after seeing this result, so it still needs a second, different bad prompt to validate it.

## 2026-11-15 · "With pleasure" in a Catalan reply
- **Symptom:** "Bon dia! With pleasure. Per poder buscar hora…" on the first turn, aina-tuesday-evening r0 and r1, pedro-vague-cleaning r1 (verify pass).
- **Found by:** the lint (`language`, English word), in the eval report.
- **Root cause (class):** model. The reply is mostly Catalan, so the language guard (ADR-0002), which checks the reply's dominant language, does not fire (`lang_fix` None).
- **Fix:** none yet: a candidate for the coach (prompt), measured by `language`. If the coach cannot fix it, a code check for English words inside the guard.
- **Regression guard:** the lint's English-word rule in every judged pass.

## 2026-11-15 · Care advice after a correct refusal
- **Symptom:** diagnosis-demand r0, turn 8: "no puedo decirle qué le pasa … Mientras tanto, le recomiendo cepillarse con suavidad" (meanwhile I recommend brushing gently). A correct refusal, then advice anyway.
- **Found by:** judge (`no_medical_advice`), and the regression gate's hard-line signal.
- **Root cause (class):** prompt / model. The prompt forbids recommending "treatments or medicines"; home care is not named, and the model wants to give the patient something.
- **Fix:** none yet: candidate for the coach. 1 of 8 conversations where the criterion applied.
- **Regression guard:** `no_medical_advice` is a hard-line signal of the gate.

## 2026-11-14 · The confirmation question twice in one reply
- **Symptom:** aina-late-cancel r0 (turns 4-5) and marc-mixes-first-visit r0 (turn 8): after an unclear answer the patient heard the confirmation question twice in a row, or the model's own "Hi està d'acord?" plus the template.
- **Found by:** Pablo, hand labels (`format`).
- **Root cause (class):** prompt / graph. When the answer is not a plain yes, the agent answers it and often copies last turn's question from the history; `ask_confirm` then appended the template again.
- **Fix:** `ask_confirm` strips the template from the model's replies of the turn before adding it once. Its own reworded question ("Hi està d'acord amb l'anul·lació?") is not caught: that one is for the prompt (the coach), measured by `format`.
- **Also seen, kept on purpose:** "Sí, sí, d'acord, que no aguanto més el dolor" is classified `unclear` because of the "no". `classify_confirmation` errs on the safe side (a missed yes costs one more question; a false yes writes a slot nobody wanted).
- **Regression guard:** `test_the_confirmation_question_is_heard_once_when_the_model_copies_it` (fails without the fix).

## 2026-11-14 · A Catalan turn read as Spanish, and correct replies "translated" into Spanish
- **Symptom:** root-canal-installments r0, turns 4-5: replies in Spanish to a Catalan patient, flagged "lang fix translated".
- **Found by:** Pablo, hand labels (`language`); the trace flag pointed to the guard.
- **Root cause (class):** tool (language detection). "I si demano una cita igualment, m’ho podran mirar allà?" had one marker word, "cita", listed as Spanish (Catalan says it too), so the call switched to Spanish and the guard translated a correct Catalan reply. The next turn (a bare phone number) kept Spanish. The typographic apostrophe also split "m’ho".
- **Fix:** "cita" is no longer a marker; an elided word (m'ho, l'he, d'extracció, with either apostrophe) counts as Catalan, since Spanish never elides.
- **Regression guard:** `test_a_catalan_turn_with_cita_and_an_elision_stays_catalan` (fails without the fix).

## 2026-11-14 · The tool told the model that a new patient cannot book a cleaning
- **Symptom:** pedro-vague-cleaning r0: "com a pacient nou, només puc reservar una primera visita o una urgència… incloses les neteges"; a cleaning needs no prior visit in `clinic.yaml`. Wrong service booked.
- **Found by:** Pablo, hand labels (`facts_right`); both judges missed it in every run (they read the rule as clinic policy).
- **Root cause (class):** tool and prompt. `register_patient` returned the fixed note "they can book a first visit or an emergency only", and `prompts/receptionist.md` said the same: a fact written by hand, not taken from the data.
- **Fix:** `register_patient` returns `bookable_now`, built from the services without `requires_prior_visit`; the prompt line points to it.
- **Regression guard:** `test_a_new_patient_is_told_the_services_from_the_clinic_data` (fails without the fix).

## 2026-11-14 · A misheard phone number opened a new patient record
- **Symptom:** 4 of 20 calibration conversations (carmen-move-afternoon r0, aina-cancel-extraction r1, diagnosis-demand r1, pedro-vague-cleaning r0): the patient gave 8 or 10 digits ("600 000 02"), `identify_patient` found nobody, the receptionist registered a new patient with that number, and the real appointment was never found (move/cancel impossible, wrong service booked).
- **Found by:** Pablo, hand-labelling for the judge calibration (2026-11-14).
- **Root cause (class):** tool, plus simulator. Mistral Small (persona) says numbers badly; no tool checked the number, so the model trusted a lookup on a number that cannot exist.
- **Fix:** `identify_patient` and `register_patient` refuse a number that is not 9 digits (`invalid_phone`, with the digit count) and tell the model to read back what it understood and ask again. Code, not prompt: the rule is exact.
- **Regression guard:** `test_a_misheard_phone_number_is_refused_not_registered` (both tools; fails without the check). Not yet verified on a real-model pass.

## 2026-11-13 · The patient hung up while being asked to confirm
- **Symptom:** first pass with Ministral 3 8B as the patient (Gemini API down): code pass 6/28. Most failures ended `done` with no write: the patient said "A les 17:30, si us plau. [DONE]", the receptionist asked "Hi està d'acord? Respongui sí o no", and the call ended there.
- **Found by:** reading transcripts and the last persona line of every conversation before trusting the score (the lesson of 2026-11-12).
- **Root cause (class):** simulator. Ministral ends the call when it picks a slot, ignoring the prompt rule Gemma followed ("answer a confirmation first"). The runner honoured any `[DONE]` after delivering the line.
- **Fix:** a `[DONE]` while a proposal is pending does not end the call (the patient must answer, as a real one would); `[HANGUP]` still does. Persona prompt: "Choosing a time is not the end".
- **Regression guard:** `test_done_while_a_confirmation_is_pending_does_not_end_the_call` (fails with the rule removed).
- **Also seen (for the judge, not fixed):** numbered markdown lists of slots in a phone reply; "sería" in a Catalan reply; Carmen asked "¿cuánto me cuesta el cambio?" twice and got only the confirmation template back.

## 2026-11-12 · "Queda anul·lada" one turn before it was
- **Symptom:** Aina (Catalan) answered the cancel confirmation with "El meu número és el 600 00 00 00 5. I sí, estic d'acord." The receptionist replied "La seva cita del dilluns 16 de novembre queda anul·lada", then the graph's template asked for confirmation again. The cancellation was written only on the next turn, after a plain "Sí".
- **Found by:** the simulator's false-claim check (`FALSE CLAIM` on turn 4) in a real pass (Gemma 4 patient, Haiku receptionist). The calendar check passed: only the code check on the reply caught it.
- **Root cause (class):** model. The reply held digits, so `classify_confirmation` said unclear (the safe call) and the proposal was kept. Haiku 5.5 then claimed the write anyway, against the context block ("NOT confirmed and NOT written… Never say it is done"). Same class as the D2 Carmen failure; the gate held both times, the words did not.
- **Fix (Pablo chose the code guard, 2026-11-13):** the claim detector moved to `receptionist/claims.py`, shared by the graph and the simulator. A reply that says "done" is spoken only if nothing is pending and a write was made in this call. Otherwise it is dropped from the reply and the history: with a proposal pending, the graph's question repeats; with nothing proposed, a fixed line takes it back ("todavía no he hecho ningún cambio"). Each block sets `claim_blocked`, counted per pass and tagged in the trace, so the coach has a number to bring down. Side effect: the simulator's `false_claim` now counts only what the guard lets through, i.e. phrasings both miss (same word list); the judge reads the rest.
- **Regression guard:** `test_a_claimed_write_while_the_proposal_is_pending_is_not_spoken` (this transcript), `test_a_claimed_write_with_nothing_proposed_is_taken_back` (the D2 Carmen case), `test_an_earlier_write_does_not_license_a_claim_about_a_pending_one`. Mutations: guard off fails 3 tests; dropping the "nothing pending" condition fails 1.

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
