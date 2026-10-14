"""Statistics used by the evaluation harness and judge calibration."""

from __future__ import annotations

import itertools
import math
from collections.abc import Sequence
from dataclasses import dataclass

Z95 = 1.959963984540054


def wilson_interval(successes: float, n: int, z: float = Z95) -> tuple[float, float]:
    """Wilson score interval for a binomial proportion; (0, 1) when there is no data."""
    if n <= 0:
        return 0.0, 1.0
    p = successes / n
    denominator = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / denominator
    margin = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / denominator
    return max(0.0, centre - margin), min(1.0, centre + margin)


def percentile(values: Sequence[float], q: float) -> float:
    """Linear-interpolated percentile, ``q`` in [0, 100]. 0.0 for an empty input."""
    if not values:
        return 0.0
    if not 0 <= q <= 100:
        raise ValueError("q must be within [0, 100]")
    ordered = sorted(values)
    position = (len(ordered) - 1) * q / 100
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return ordered[lower]
    return ordered[lower] + (ordered[upper] - ordered[lower]) * (position - lower)


def quadratic_weighted_kappa(
    rater_a: Sequence[int], rater_b: Sequence[int], *, low: int = 1, high: int = 5
) -> float:
    """Cohen's kappa with quadratic weights for ordinal ratings in [low, high]."""
    if len(rater_a) != len(rater_b):
        raise ValueError("raters must score the same items")
    if not rater_a:
        raise ValueError("no ratings")
    k = high - low + 1
    observed = [[0.0] * k for _ in range(k)]
    for a, b in zip(rater_a, rater_b, strict=True):
        if not (low <= a <= high and low <= b <= high):
            raise ValueError(f"ratings must be within [{low}, {high}]")
        observed[a - low][b - low] += 1
    n = float(len(rater_a))
    hist_a = [sum(row) for row in observed]
    hist_b = [sum(observed[i][j] for i in range(k)) for j in range(k)]
    numerator = 0.0
    denominator = 0.0
    for i in range(k):
        for j in range(k):
            weight = ((i - j) ** 2) / ((k - 1) ** 2)
            expected = hist_a[i] * hist_b[j] / n
            numerator += weight * observed[i][j]
            denominator += weight * expected
    if denominator == 0:
        # Both raters used a single identical category: perfect agreement.
        return 1.0 if numerator == 0 else 0.0
    return 1.0 - numerator / denominator


def _ranks(values: Sequence[float]) -> list[float]:
    order = sorted(range(len(values)), key=lambda i: values[i])
    ranks = [0.0] * len(values)
    i = 0
    while i < len(order):
        j = i
        while j + 1 < len(order) and values[order[j + 1]] == values[order[i]]:
            j += 1
        average = (i + j) / 2 + 1
        for idx in order[i : j + 1]:
            ranks[idx] = average
        i = j + 1
    return ranks


def spearman(xs: Sequence[float], ys: Sequence[float]) -> float:
    """Spearman rank correlation with average ranks for ties; 0.0 without variance."""
    if len(xs) != len(ys):
        raise ValueError("inputs must have equal length")
    if len(xs) < 2:
        return 0.0
    rx, ry = _ranks(xs), _ranks(ys)
    mx, my = sum(rx) / len(rx), sum(ry) / len(ry)
    cov = sum((a - mx) * (b - my) for a, b in zip(rx, ry, strict=True))
    vx = math.sqrt(sum((a - mx) ** 2 for a in rx))
    vy = math.sqrt(sum((b - my) ** 2 for b in ry))
    if vx == 0 or vy == 0:
        return 0.0
    return cov / (vx * vy)


@dataclass
class _Block:
    total: float
    weight: float
    xs: list[float]

    @property
    def mean(self) -> float:
        return self.total / self.weight


def isotonic_fit(xs: Sequence[float], ys: Sequence[float]) -> list[tuple[float, float]]:
    """Pool-adjacent-violators isotonic regression.

    Returns non-decreasing (x, y) breakpoints, one per distinct x, suitable
    for piecewise-linear interpolation with :func:`isotonic_predict`.
    """
    if len(xs) != len(ys):
        raise ValueError("inputs must have equal length")
    if not xs:
        raise ValueError("no data")
    grouped: dict[float, list[float]] = {}
    for x, y in zip(xs, ys, strict=True):
        grouped.setdefault(x, []).append(y)
    blocks: list[_Block] = []
    for x in sorted(grouped):
        values = grouped[x]
        blocks.append(_Block(total=sum(values), weight=float(len(values)), xs=[x]))
        while len(blocks) > 1 and blocks[-2].mean > blocks[-1].mean:
            last = blocks.pop()
            prev = blocks[-1]
            blocks[-1] = _Block(
                total=prev.total + last.total,
                weight=prev.weight + last.weight,
                xs=prev.xs + last.xs,
            )
    return [(x, block.mean) for block in blocks for x in block.xs]


def isotonic_predict(points: Sequence[tuple[float, float]], x: float) -> float:
    """Piecewise-linear interpolation through isotonic breakpoints, clamped at the ends."""
    if not points:
        raise ValueError("empty mapping")
    if x <= points[0][0]:
        return points[0][1]
    if x >= points[-1][0]:
        return points[-1][1]
    for (x0, y0), (x1, y1) in itertools.pairwise(points):
        if x0 <= x <= x1:
            if x1 == x0:
                return y1
            return y0 + (y1 - y0) * (x - x0) / (x1 - x0)
    return points[-1][1]  # pragma: no cover - unreachable for sorted points
