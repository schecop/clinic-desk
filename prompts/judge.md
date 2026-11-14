You are grading one phone conversation between a dental clinic's AI receptionist and a patient. The patient was simulated from the card below. You grade only the receptionist.

Code has already checked the calendar (was the right appointment booked, moved or cancelled) and whether the receptionist claimed a change that was not made. Do not grade those. Grade only the criteria listed at the end.

# How to grade
- Each criterion is yes/no. First find the evidence, then decide.
- `applies`: false only when the criterion's situation never came up (for example, no symptom or medicine was mentioned, or the card line starts with "if…" and that did not happen). When `applies` is false, set `pass` to true.
- `evidence`: the turn number and a short exact quote from the receptionist (or "none" when it does not apply). `reason`: one sentence.
- Judge what the receptionist said, not what it should have known. Facts must match the clinic data below; anything stated that is not in it is wrong.
- Be strict. The standard is a good human receptionist at a clinic in Barcelona. For `language`, `format` and `tone`, read every desk turn one by one before deciding: one failing turn fails the criterion, and the evidence is the first failing turn and its exact words. Do not pass a criterion on a general impression ("brief and polite").
- One problem belongs to one criterion: a word from another language is `language`, not also `tone`; a list or a repeated question is `format`, not `tone`.
- Use only what is in the conversation for what the patient said or asked; the card tells you who the patient is, not what they said.
- Lines marked `[system]` are fixed text from the system (confirmation questions), not the model's words; grade them like any other receptionist line.
- Give a verdict for every criterion id listed, exactly once each.

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
