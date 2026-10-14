# ADR-0004: Calibrated judges and position-debiased pairwise ranking

- Status: Accepted
- Date: 2026-10-14

## Context

Open-ended tasks (summaries, general assistance) cannot be graded by exact
match. LLM judges are cheap but have known failure modes: disagreement with
human raters, a preference for the first answer shown, a preference for their
own outputs, and susceptibility to instructions embedded in the answer they
grade. Promotion decisions must not rest on an unmeasured judge.

## Decision

- **Calibration:** a judge is calibrated per (judge provider, rubric digest)
  against human 1 to 5 scores. We compute quadratic-weighted kappa, Spearman
  correlation, exact agreement and mean bias, and fit an isotonic
  (pool-adjacent-violators) map from raw judge score to expected human score.
  A calibration counts only with at least `MRE_MIN_CALIBRATION_ITEMS` items
  and kappa of at least `MRE_MIN_JUDGE_KAPPA`.
- Judge-graded runs apply the latest calibrated map and report
  `judge_calibrated`. Promotion gates with `require_calibrated_judge` refuse
  runs whose judge was not calibrated.
- **Independence:** a judge is never one of the providers it grades.
- **Manipulation:** judged outputs are spotlighted between random boundaries
  and scanned with agentic-core's injection detector; an output that tries to
  instruct the judge fails without a judge call.
- **Pairwise:** each pair is judged in both presentation orders and
  disagreement becomes a tie. Strengths are fitted with the Bradley-Terry model
  using Hunter's MM algorithm with 0.1 virtual ties per pair; 95% intervals
  come from a bootstrap that resamples golden cases, because comparisons on
  the same case share a prompt and are not independent.

## Consequences

- Pairwise ranking costs two judge calls per pair per case; the run cost
  estimate includes them and governance caps the total.
- Ties absorb position bias instead of turning it into false wins.
- Changing a rubric changes its digest and therefore needs a new calibration.
