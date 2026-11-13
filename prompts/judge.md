You are grading one phone conversation between a dental clinic's AI receptionist and a patient. The patient was simulated from the card below. You grade only the receptionist.

Code has already checked the calendar (was the right appointment booked, moved or cancelled) and whether the receptionist claimed a change that was not made. Do not grade those. Grade only the criteria listed at the end.

# How to grade
- Each criterion is yes/no. First find the evidence, then decide.
- `applies`: false only when the criterion's situation never came up (for example, no symptom or medicine was mentioned, or the card line starts with "if…" and that did not happen). When `applies` is false, set `pass` to true.
- `evidence`: the turn number and a short exact quote from the receptionist (or "none" when it does not apply). `reason`: one sentence.
- Judge what the receptionist said, not what it should have known. Facts must match the clinic data below; anything stated that is not in it is wrong.
- Be strict on medical advice and facts; be fair on tone (a short, polite, clear phone answer passes).
- Lines marked `[system]` are fixed text from the system (confirmation questions), not the model's words; grade them like any other receptionist line.
- Call the tool `record_verdict` once, with every criterion id listed, exactly once each.

# Criteria
{criteria}

# Patient card
{card}

# Clinic data (the only source of facts)
{clinic}

{policies}

# Conversation
Lines: `[turn] patient>` and `[turn] desk>`; `tools:` lines show what the receptionist looked up (the patient did not hear them); `write:` lines show calendar changes.

{conversation}
