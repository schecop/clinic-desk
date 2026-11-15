# ADR-0006: The coach loop and its keep rule

- **Date:** 2026-11-15
- **Status:** accepted

## Context
The coach improves `prompts/receptionist.md` by itself: it reads failed conversations, proposes a change, re-runs the simulator and keeps or rolls back the change. Each measurement costs ~$0.45 (visible) + ~$0.20 (held-out), and a pass of the same prompt moves several points by chance (ADR-0004). The coach must not overfit to the conversations it reads, and must never see the held-out personas.

## Decision
`clinic-desk coach` (`src/clinic_desk/coach/`):
- **One change per iteration**, as an exact replacement (`old` → `new`) with a target criterion, the failures it addresses, a reason and a risk. Code rejects it before anything is spent if `old` does not appear exactly once, if it adds more than 1,200 characters, or if `new` names a patient (overfitting). A rejected answer is retried once with the reason.
- **Keep rule, fixed before the first run (Pablo):** the target criterion rises by z ≥ 2 on the visible pass, the regression gate (ADR-0004) sees no regression on visible, and then the gate sees no regression on held-out. Held-out is measured only when visible wins, and its baseline only when first needed.
- **What the coach reads:** the current prompt, the visible failures grouped by criterion (evidence and reason, 6 per criterion), the rubric and lint rules, and its earlier attempts. Held-out runs, personas and scores never reach it; a held-out rollback reads "it regressed on patients you do not see".
- **Kept:** the candidate and its passes become the new best and baselines. The change log (`runs/coach/<id>/changelog.jsonl` + `.md`) records every iteration: diff, reason, risk, scores before and after, decision and why.
- `prompts/receptionist.md` is never written by the loop: the best prompt goes to `best.md`, for a person to review and commit.
- Model: Claude Sonnet 5.5, effort medium (ADR-0005); ~$0.02 per proposal.

## Alternatives considered
- **Gain on the whole score (z ≥ 2 on all criteria):** one change rarely moves the total by ~10 points, so almost nothing would be kept.
- **Re-measure the baseline every iteration:** halves the noise of a comparison but doubles the cost; the z-test already accounts for two independent passes.
- **Let the coach rewrite the whole prompt:** no attribution of a gain to a change, and much harder to review.

## Consequences
- **Winner's curse:** a kept candidate's pass was selected for scoring high, so it is an optimistic baseline for the next iteration; later changes have to beat a lucky draw. It errs on the side of keeping less.
- The held-out check is weak with 6 personas × 2 repeats (12 conversations, ~15-20 points of noise per criterion). First run: visible 65% → 77% (z 2.6); held-out, re-measured with 4 more repeats and pooled (~33 conversations per prompt), 69% → 75% (z 1.3, not proven). The tone change replicated on held-out (28% → 91%, z 5.2); the language change did not transfer (19% → 21%). Measure a final prompt with more held-out repeats before trusting it, and grow the held-out set (backlog).
- Examples in a coach change are copied by the receptionist, sometimes where they do not apply (first run: a first-visit reminder on a checkup). Reviewing `best.md` before committing it is part of the loop.
