"""Sufficient statistics for one routing arm (task family, input bucket, provider).

Every field is a weighted sum, so observations combine by addition and
concurrent writers never conflict.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, fields

FAMILY_BUCKET = "*"


@dataclass(frozen=True)
class ArmStats:
    quality_n: float = 0.0
    quality_sum: float = 0.0
    latency_n: float = 0.0
    latency_log_sum: float = 0.0
    latency_log_sumsq: float = 0.0
    cost_n: float = 0.0
    cost_sum: float = 0.0

    def plus(self, other: ArmStats, *, quality: float, latency: float, cost: float) -> ArmStats:
        """Add ``other`` with a separate scale per dimension (used for priors)."""
        return ArmStats(
            quality_n=self.quality_n + quality * other.quality_n,
            quality_sum=self.quality_sum + quality * other.quality_sum,
            latency_n=self.latency_n + latency * other.latency_n,
            latency_log_sum=self.latency_log_sum + latency * other.latency_log_sum,
            latency_log_sumsq=self.latency_log_sumsq + latency * other.latency_log_sumsq,
            cost_n=self.cost_n + cost * other.cost_n,
            cost_sum=self.cost_sum + cost * other.cost_sum,
        )

    @property
    def quality_mean(self) -> float:
        return (1.0 + self.quality_sum) / (2.0 + self.quality_n)

    @property
    def latency_median_ms(self) -> float | None:
        if self.latency_n <= 0:
            return None
        return math.exp(self.latency_log_sum / self.latency_n)

    @property
    def cost_mean_usd(self) -> float | None:
        return self.cost_sum / self.cost_n if self.cost_n > 0 else None


@dataclass(frozen=True)
class Observation:
    """One outcome for one arm. Any dimension may be absent."""

    quality: float | None = None
    latency_ms: float | None = None
    cost_usd: float | None = None
    weight: float = 1.0

    def __post_init__(self) -> None:
        if self.quality is not None and not 0.0 <= self.quality <= 1.0:
            raise ValueError("quality must be in [0, 1]")
        if self.latency_ms is not None and self.latency_ms < 0:
            raise ValueError("latency must be non-negative")
        if self.cost_usd is not None and self.cost_usd < 0:
            raise ValueError("cost must be non-negative")
        if self.weight <= 0:
            raise ValueError("weight must be positive")

    def as_delta(self) -> ArmStats:
        w = self.weight
        delta: dict[str, float] = {}
        if self.quality is not None:
            delta |= {"quality_n": w, "quality_sum": w * self.quality}
        if self.latency_ms is not None:
            log_latency = math.log(max(self.latency_ms, 1.0))
            delta |= {
                "latency_n": w,
                "latency_log_sum": w * log_latency,
                "latency_log_sumsq": w * log_latency * log_latency,
            }
        if self.cost_usd is not None:
            delta |= {"cost_n": w, "cost_sum": w * self.cost_usd}
        return ArmStats(**delta)


STAT_FIELDS = tuple(f.name for f in fields(ArmStats))
