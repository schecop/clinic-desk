# ADR-0004: Regression gate pooled per criterion, with a floor for hard lines

- **Date:** 2026-11-15
- **Status:** accepted

## Context
A prompt change must not ship (or be kept by the coach) if it breaks what the current prompt does. The gate compares a candidate pass with a baseline pass: 14 visible personas × 2 repeats, judged (Gemini 3.8 Flash + lint). It has to tell a real regression from noise: the persona model, the receptionist and the judge all vary between passes, and a pass costs ~$0.45, so more repeats are expensive, for the gate and for every coach iteration.

## Decision
`clinic-desk gate BASELINE CANDIDATE` (free, reads saved runs, exit 1 on failure).
- Each criterion's pass rate is **pooled over every persona and repeat**, and a two-proportion z-test breaks it at a drop of **z ≥ 2**. Code checks, false claims and all criteria together are signals as well.
- **Hard lines** (`no_medical_advice`, `no_false_claims`) also break with **2+ failures and a higher failure rate than the baseline**, since they apply in 4-8 conversations, too few for z.
- The report names each broken signal, the personas where it failed more often than in the baseline, and one piece of evidence with its trace per persona.
- Runs locally. It is not a CI job: a gate check costs money, and the budget rule says no paid pass without stating its cost.

## Alternatives considered
- **Per persona (mean drop beyond max(10 points, baseline spread)):** failed 6 of 14 personas on two near-identical passes (65% → 65%). Two repeats cannot estimate a persona's noise.
- **Per persona with 4-5 repeats:** ~$0.90-1.10 per check, paid again on every coach iteration. Over budget.
- **Hard lines at zero tolerance (any increase fails):** fails the near-identical pair on one rare, already existing bug (a single "le recomiendo cepillarse"), so it would block every change until that bug is fixed.
- **GitHub Actions on `prompts/` changes:** needs paid keys in CI and spends money on every push.

## Consequences
- A break confined to one persona can hide in the pool unless it is on a hard line. The per-persona failures still appear as evidence.
- The hard-line floor was set **after** seeing the first bad-prompt run (paracetamol in 2 of 4 conversations, z 1.4). Validate it on a second, different bad prompt before trusting it fully.
- With ~8 signals at z ≥ 2, about 1 in 6 comparisons of identical prompts fails somewhere by chance. Acceptable for a gate (re-run on doubt), too loose as the coach's keep rule, which needs its own threshold.
- More medical personas (or more repeats for them only) would let the z-test cover the hard lines too.
