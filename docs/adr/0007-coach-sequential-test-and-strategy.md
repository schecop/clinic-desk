# ADR-0007: Sequential measurement and a more strategic coach

- **Date:** 2026-11-15
- **Status:** accepted (revises the keep rule and the coach's input in ADR-0006)

## Context
The second coach run (`runs/coach/20261115-180938-coach`, 3 iterations) kept nothing, although every candidate raised the total on visible by 5-8 points. Its best candidate raised `format` from 56% to 75% (+19 points) and scored z 1.5: with 2 repeats (~27 conversations per pass), a real gain of that size is detected well under half the time. About 100 conversations per side would be needed to detect it 8 times in 10, and the budget cannot pay that for every candidate (~$0.45 per 2 repeats).

The coach also chose badly. It did not target the biggest pattern ("con gusto" in the first Spanish reply: 7 of 7 Spanish `language` failures), it kept a prohibition that was not working, and one of its targets was text added by code (the confirmation question), which no prompt change can fix. A fixed-gender example from an earlier change had been copied word for word to male patients (FAILURES, 2026-11-15).

## Decision
Fixed before the next run (Pablo, 2026-11-15).

**Measurement (`coach/loop.py`, `Plan`):**
- The start prompt is measured on visible with **4 repeats**. It is the reference for every iteration, so the cost is shared.
- A candidate gets **2 repeats**. If its target rose by **1 ≤ z < 2.2** (promising), it gets **2 more** in the same pass and is decided on all 4. Below z 1 it is rolled back without spending more.
- **Keep:** the target rises by **z ≥ 2.2**, the gate sees no regression on visible, and none on held-out, now measured with **4 repeats** for both the start and the candidate. The held-out start is measured only once a candidate wins on visible.
- z 2.2 instead of 2 because a promising candidate is looked at twice, which gives it two chances to pass by luck. The one-sided false-positive rate per look drops from ~2.3% to ~1.4%, so the two looks together stay close to the old single look.

**Coach (`prompts/coach.md`, `coach/propose.py`):**
- It reads the failures counted per criterion (conversations, patients, categories), the lint's exact patterns counted by code, and the judge's findings with each patient's category.
- Strategy, in order: group into patterns; diagnose the cause (missing rule, ignored rule, copied example, or not the prompt: code, judge or simulator); pick the biggest pattern a prompt change can fix; check the history.
- Writing: say what to do, not only what to avoid; rewrite an existing rule instead of adding a second one; neutral examples: placeholders for data from a tool or the patient ("el [día] a las [hora]"), gender-free wording or both forms (Pablo, after the first run copied "lunes a las 9:00" into the prompt).
- New fields: `pattern`, `cause`, `elsewhere` (failures it thinks are not for the prompt, logged for a person to review).
- **Code check against overfitting:** the conversations listed in `failures` must be real failures of the target, from at least 3 patients in at least 2 persona categories. Otherwise the proposal is sent back before anything is spent.

## Alternatives considered
- **4 repeats for every candidate:** twice the cost per iteration (~$0.9), and most of it spent on candidates that are clearly not working.
- **Keep z ≥ 2 with sequential looks:** with two looks, the false-positive rate is higher than with one; raising the bar to 2.2 compensates for that.
- **Lower the bar (z ≥ 1.5):** more gains kept, but also more noise kept, and noise compounds across iterations (winner's curse).
- **Paired comparison per persona:** more power for the same cost, but persona replies differ from run to run, so the pairs are only loosely matched. Left for later.

## Consequences
- Medium gains (around +15-20 points on a criterion) have a real chance to be kept. Tiny ones still cannot be measured, and the coach is told so.
- A coach run costs more up front (the start is ~$0.9 instead of ~$0.45) and less per rejected candidate.
- A kept candidate's pass becomes the next baseline with 2 or 4 repeats, so later iterations have less power than the first. Winner's curse as in ADR-0006.
- The held-out check with 4 repeats per side (~24 conversations) doubles as the final held-out measurement for a winner.
- **First run (`runs/coach/20261115-185938-coach`, cut after 2 of 3 iterations by a Mistral outage, ~$2.2):** the start was topped up from 2 to 4 repeats (reused). Iteration 1 (language: "claro/por supuesto" instead of "con gusto") rose z 0.4 and was rolled back without more repeats. Iteration 2 (format: options in one spoken sentence, no lists; the coach counted 15 list failures across 8+ patients) rose z 2.8 on the first look and was kept: visible format 55% → 86%; **held-out format 46% → 92% (z 3.4)**, held-out all criteria 79% → 86% (z 1.6, not proven), no regression. `language` fell on both splits (visible 45% → 32%, held-out 71% → 54%, z 1.2 each): within the noise, but in the same direction twice; to watch. The kept example names days and times, against the coach's own instructions.

