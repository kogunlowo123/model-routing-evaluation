# ADR-0001: Contextual Thompson sampling with additive sufficient statistics

- Status: Accepted
- Date: 2026-10-14

## Context

Choosing a model per request is an explore/exploit problem. Static rules
(always vendor X for task Y) never discover that a cheaper provider passes,
and never notice a regression. The router runs on several replicas, so any
learning state must tolerate concurrent writers without locks. Signals arrive
at different times and with different reliability: latency and cost
immediately, a schema grade immediately when the caller supplies a schema,
caller feedback later, shadow and offline results in batches.

## Decision

- One arm per (task family, input-size bucket, provider). The bucket
  (`short` or `long` at `long_input_chars`) is the context: quality and
  latency differ materially with input length.
- Posteriors: quality Beta(1 + sum, 1 + n - sum) with fractional rewards; log
  latency Normal-Inverse-Gamma with a prior centred on the family latency
  target; cost is the observed mean (or the price-sheet estimate before any
  observation) multiplied by Gamma(k, 1/k) noise with k = 1 + n.
- Each draw is scored with the family utility
  `wq*q + wl/(1+lat/target_lat) + wc/(1+cost/target_cost) + wcomp*posture`
  and providers are tried in descending sampled utility, which is also the
  failover order.
- State is stored as additive sufficient statistics (counts, sums, sums of
  squares) and updated with `x = x + delta` statements, once for the context
  bucket and once for the family bucket `*`.
- Sparse contexts borrow strength: the family arm is added as a prior scaled
  to at most `prior_pseudo_observations` observations (empirical Bayes).
- Provider failures record quality 0; shadow pairwise outcomes record
  relative rewards for both arms at weight 0.5; offline results are added to
  the family arm at `offline_prior_weight`.

## Consequences

- Replicas never coordinate; updates commute, and losing a replica loses no
  learning state.
- Exploration falls as evidence accumulates, without a tuned epsilon.
- Utility weights are explicit, reviewable configuration per family.
- The model is stationary: a vendor regression is learned at the rate new
  evidence outweighs old. Shadow traffic and scheduled offline runs bound that
  delay; a discounting scheme can be added later without changing storage.
- Relative shadow rewards approximate dueling-bandit feedback; their weight is
  deliberately below that of absolute grades.
