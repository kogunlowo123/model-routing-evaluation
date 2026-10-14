"""Bradley-Terry ranking from pairwise preferences.

P(i beats j) = p_i / (p_i + p_j). Strengths are fitted with Hunter's (2004)
minorisation-maximisation updates. Ties count as half a win for each side.
Every pair receives ``prior_ties`` virtual ties so that a player with no wins
keeps a finite, positive strength and the fit always converges.
Confidence intervals come from a case-level bootstrap: comparisons from the
same golden case are resampled together because they share a prompt.
"""

from __future__ import annotations

import random
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from model_routing_evaluation.evaluation.statistics import percentile


@dataclass(frozen=True)
class Comparison:
    """``result`` is 1.0 when ``a`` wins, 0.0 when ``b`` wins, 0.5 for a tie."""

    group: str
    a: str
    b: str
    result: float

    def __post_init__(self) -> None:
        if self.a == self.b:
            raise ValueError("a player cannot be compared with itself")
        if self.result not in (0.0, 0.5, 1.0):
            raise ValueError("result must be 0.0, 0.5 or 1.0")


@dataclass(frozen=True)
class Ranking:
    strengths: Mapping[str, float]
    intervals: Mapping[str, tuple[float, float]]
    wins: Mapping[str, float]
    comparisons: Mapping[str, int]

    def ordered(self) -> list[str]:
        return sorted(self.strengths, key=lambda p: (-self.strengths[p], p))


def fit(
    players: Sequence[str],
    comparisons: Sequence[Comparison],
    *,
    prior_ties: float = 0.1,
    tolerance: float = 1e-10,
    max_iterations: int = 10_000,
) -> dict[str, float]:
    """Strengths normalised to sum to 1."""
    names = sorted(set(players))
    if len(names) < 2:
        raise ValueError("ranking needs at least two players")
    index = {p: i for i, p in enumerate(names)}
    k = len(names)
    wins = [[0.0] * k for _ in range(k)]
    for c in comparisons:
        if c.a not in index or c.b not in index:
            raise ValueError(f"comparison references unknown player: {c.a} / {c.b}")
        i, j = index[c.a], index[c.b]
        wins[i][j] += c.result
        wins[j][i] += 1.0 - c.result
    for i in range(k):
        for j in range(k):
            if i != j:
                wins[i][j] += prior_ties / 2
    games = [[wins[i][j] + wins[j][i] for j in range(k)] for i in range(k)]
    total_wins = [sum(row) for row in wins]

    strength = [1.0 / k] * k
    for _ in range(max_iterations):
        updated = []
        for i in range(k):
            denominator = sum(games[i][j] / (strength[i] + strength[j]) for j in range(k) if j != i)
            updated.append(total_wins[i] / denominator)
        norm = sum(updated)
        updated = [s / norm for s in updated]
        delta = max(abs(a - b) for a, b in zip(updated, strength, strict=True))
        strength = updated
        if delta < tolerance:
            break
    return {p: strength[index[p]] for p in names}


def rank(
    players: Sequence[str],
    comparisons: Sequence[Comparison],
    *,
    rng: random.Random,
    bootstrap_rounds: int = 200,
) -> Ranking:
    strengths = fit(players, comparisons)
    wins: dict[str, float] = dict.fromkeys(strengths, 0.0)
    counts: dict[str, int] = dict.fromkeys(strengths, 0)
    groups: dict[str, list[Comparison]] = {}
    for c in comparisons:
        wins[c.a] += c.result
        wins[c.b] += 1.0 - c.result
        counts[c.a] += 1
        counts[c.b] += 1
        groups.setdefault(c.group, []).append(c)

    samples: dict[str, list[float]] = {p: [] for p in strengths}
    keys = sorted(groups)
    if keys and bootstrap_rounds > 0:
        for _ in range(bootstrap_rounds):
            resampled = [c for _ in keys for c in groups[keys[rng.randrange(len(keys))]]]
            for player, value in fit(players, resampled).items():
                samples[player].append(value)
    intervals = {
        p: (
            (percentile(samples[p], 2.5), percentile(samples[p], 97.5))
            if samples[p]
            else (strengths[p], strengths[p])
        )
        for p in strengths
    }
    return Ranking(strengths=strengths, intervals=intervals, wins=wins, comparisons=counts)
