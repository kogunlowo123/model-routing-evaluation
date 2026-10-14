"""Contextual Thompson sampling over a quality / latency / cost / compliance utility.

Each (task family, input-size bucket, provider) arm keeps additive sufficient
statistics, so updates are commutative ``x = x + delta`` writes that many
replicas can apply concurrently without coordination.

Posteriors per arm:

* quality in [0, 1]: Beta(1 + sum, 1 + n - sum) with fractional rewards
  (graded scores are treated as fractional Bernoulli outcomes);
* log latency: Normal-Inverse-Gamma, prior centred on the family's latency
  target, so an unmeasured provider is assumed to just meet the target;
* cost per request: observed mean (or the price-sheet estimate before any
  observation) with multiplicative Gamma noise that shrinks as 1/(1+n).

Sparse contexts borrow strength from the family-level arm (bucket ``*``):
its statistics are added as a prior capped at ``prior_pseudo_observations``
pseudo-observations (empirical Bayes shrinkage).
"""

from __future__ import annotations

import math
import random
from dataclasses import dataclass

from model_routing_evaluation.domain.stats import ArmStats
from model_routing_evaluation.routing.config import FamilyConfig, UtilityWeights

# Normal-Inverse-Gamma prior on log latency: one pseudo-observation for the
# mean, log-scale standard deviation 0.5 (a factor of ~1.65 either way).
_KAPPA0 = 1.0
_ALPHA0 = 2.0
_BETA0 = 0.5


@dataclass(frozen=True)
class Sample:
    quality: float
    latency_ms: float
    cost_usd: float


def utility(
    *,
    quality: float,
    latency_ms: float,
    cost_usd: float,
    posture: float,
    family: FamilyConfig,
) -> float:
    """Scalar utility in [0, 1]. Latency and cost at their targets score 0.5."""
    w: UtilityWeights = family.weights
    latency_score = 1.0 / (1.0 + latency_ms / family.latency_target_ms)
    cost_score = 1.0 / (1.0 + cost_usd / family.cost_target_usd)
    return (
        w.quality * quality
        + w.latency * latency_score
        + w.cost * cost_score
        + w.compliance * posture
    )


class ThompsonSampler:
    def __init__(self, rng: random.Random, *, prior_pseudo_observations: float = 10.0) -> None:
        self._rng = rng
        self._m = prior_pseudo_observations

    def posterior(self, context: ArmStats, family_level: ArmStats) -> ArmStats:
        """Context statistics plus the family arm as a prior of at most m observations."""

        def scale(n: float) -> float:
            return min(1.0, self._m / n) if n > 0 else 0.0

        return context.plus(
            family_level,
            quality=scale(family_level.quality_n),
            latency=scale(family_level.latency_n),
            cost=scale(family_level.cost_n),
        )

    def sample(
        self, stats: ArmStats, *, latency_target_ms: float, estimated_cost_usd: float
    ) -> Sample:
        rng = self._rng
        quality = rng.betavariate(
            1.0 + stats.quality_sum, 1.0 + max(stats.quality_n - stats.quality_sum, 0.0)
        )

        mu0 = math.log(latency_target_ms)
        n = stats.latency_n
        if n > 0:
            mean = stats.latency_log_sum / n
            spread = max(stats.latency_log_sumsq - n * mean * mean, 0.0)
        else:
            mean, spread = mu0, 0.0
        kappa_n = _KAPPA0 + n
        mu_n = (_KAPPA0 * mu0 + n * mean) / kappa_n
        alpha_n = _ALPHA0 + n / 2.0
        beta_n = _BETA0 + spread / 2.0 + _KAPPA0 * n * (mean - mu0) ** 2 / (2.0 * kappa_n)
        variance = 1.0 / rng.gammavariate(alpha_n, 1.0 / beta_n)
        log_latency = rng.gauss(mu_n, math.sqrt(variance / kappa_n))
        latency_ms = math.exp(min(log_latency, 20.0))

        base_cost = stats.cost_mean_usd if stats.cost_n > 0 else estimated_cost_usd
        shape = 1.0 + stats.cost_n
        cost_usd = (base_cost or 0.0) * rng.gammavariate(shape, 1.0 / shape)
        return Sample(quality=quality, latency_ms=latency_ms, cost_usd=cost_usd)

    def expected(
        self, stats: ArmStats, *, latency_target_ms: float, estimated_cost_usd: float
    ) -> Sample:
        """Posterior point estimates, used for reports and the latency/cost tie-breaks."""
        latency = stats.latency_median_ms or latency_target_ms
        cost = stats.cost_mean_usd
        return Sample(
            quality=stats.quality_mean,
            latency_ms=latency,
            cost_usd=estimated_cost_usd if cost is None else cost,
        )
