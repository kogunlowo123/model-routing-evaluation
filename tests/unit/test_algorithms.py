"""Bandit, statistics and Bradley-Terry: the numerical core."""

from __future__ import annotations

import itertools
import math
import random

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from model_routing_evaluation.domain.stats import ArmStats, Observation
from model_routing_evaluation.evaluation import bradley_terry
from model_routing_evaluation.evaluation.statistics import (
    isotonic_fit,
    isotonic_predict,
    percentile,
    quadratic_weighted_kappa,
    spearman,
    wilson_interval,
)
from model_routing_evaluation.routing.bandit import ThompsonSampler, utility
from model_routing_evaluation.routing.config import FamilyConfig, UtilityWeights


def family(**weights: float) -> FamilyConfig:
    w = {"quality": 0.6, "latency": 0.2, "cost": 0.1, "compliance": 0.1} | weights
    return FamilyConfig(
        description="test family",
        weights=UtilityWeights(**w),
        latency_target_ms=1000,
        cost_target_usd=0.01,
        pool=("a",),
    )


class TestObservations:
    def test_delta_accumulates_sufficient_statistics(self) -> None:
        delta = Observation(quality=0.5, latency_ms=100.0, cost_usd=0.02, weight=2.0).as_delta()
        assert delta.quality_n == 2.0
        assert delta.quality_sum == 1.0
        assert delta.latency_log_sum == pytest.approx(2 * math.log(100))
        assert delta.latency_log_sumsq == pytest.approx(2 * math.log(100) ** 2)
        assert delta.cost_sum == pytest.approx(0.04)
        assert Observation().as_delta() == ArmStats()

    @pytest.mark.parametrize(
        "kwargs",
        [
            {"quality": 1.5},
            {"quality": -0.1},
            {"latency_ms": -1.0},
            {"cost_usd": -1.0},
            {"weight": 0},
        ],
    )
    def test_rejects_invalid_observations(self, kwargs: dict[str, float]) -> None:
        with pytest.raises(ValueError, match="must be"):
            Observation(**kwargs)

    def test_point_estimates(self) -> None:
        stats = ArmStats(
            quality_n=8,
            quality_sum=6,
            latency_n=2,
            latency_log_sum=2 * math.log(200),
            cost_n=4,
            cost_sum=0.4,
        )
        assert stats.quality_mean == pytest.approx(0.7)
        assert stats.latency_median_ms == pytest.approx(200)
        assert stats.cost_mean_usd == pytest.approx(0.1)
        assert ArmStats().latency_median_ms is None
        assert ArmStats().cost_mean_usd is None


class TestThompsonSampler:
    def test_posterior_caps_family_prior(self) -> None:
        sampler = ThompsonSampler(random.Random(1), prior_pseudo_observations=10)
        context = ArmStats(quality_n=2, quality_sum=2)
        family_level = ArmStats(quality_n=100, quality_sum=20)
        blended = sampler.posterior(context, family_level)
        assert blended.quality_n == pytest.approx(12)
        assert blended.quality_sum == pytest.approx(4)
        assert sampler.posterior(context, ArmStats()) == context

    def test_sampling_is_reproducible_and_bounded(self) -> None:
        stats = ArmStats(
            quality_n=10,
            quality_sum=9,
            latency_n=10,
            latency_log_sum=10 * math.log(500),
            latency_log_sumsq=10 * math.log(500) ** 2,
            cost_n=10,
            cost_sum=0.1,
        )
        first = ThompsonSampler(random.Random(3)).sample(
            stats, latency_target_ms=1000, estimated_cost_usd=0.05
        )
        second = ThompsonSampler(random.Random(3)).sample(
            stats, latency_target_ms=1000, estimated_cost_usd=0.05
        )
        assert first == second
        assert 0 <= first.quality <= 1
        assert first.latency_ms > 0
        assert first.cost_usd >= 0

    def test_unobserved_arm_uses_targets_and_estimates(self) -> None:
        sampler = ThompsonSampler(random.Random(5))
        latencies = [
            sampler.sample(ArmStats(), latency_target_ms=2000, estimated_cost_usd=0.01).latency_ms
            for _ in range(400)
        ]
        median = sorted(latencies)[200]
        assert 1000 < median < 4000
        expected = sampler.expected(ArmStats(), latency_target_ms=2000, estimated_cost_usd=0.03)
        assert (expected.quality, expected.latency_ms, expected.cost_usd) == (0.5, 2000, 0.03)

    def test_better_arm_wins_most_draws(self) -> None:
        sampler = ThompsonSampler(random.Random(11))
        good = ArmStats(quality_n=40, quality_sum=36)
        bad = ArmStats(quality_n=40, quality_sum=12)
        fam = family()
        wins = 0
        for _ in range(300):
            draws = []
            for stats in (good, bad):
                s = sampler.sample(stats, latency_target_ms=1000, estimated_cost_usd=0.01)
                draws.append(
                    utility(
                        quality=s.quality,
                        latency_ms=s.latency_ms,
                        cost_usd=s.cost_usd,
                        posture=0.5,
                        family=fam,
                    )
                )
            wins += draws[0] > draws[1]
        assert wins > 270

    def test_utility_scores_targets_at_half(self) -> None:
        fam = family(quality=0.0, latency=0.5, cost=0.5, compliance=0.0)
        assert utility(
            quality=1, latency_ms=1000, cost_usd=0.01, posture=1, family=fam
        ) == pytest.approx(0.5)
        free_and_instant = utility(quality=0, latency_ms=0, cost_usd=0, posture=0, family=fam)
        assert free_and_instant == pytest.approx(1.0)

    def test_weights_must_sum_to_one(self) -> None:
        with pytest.raises(ValueError, match="sum to 1"):
            UtilityWeights(quality=0.5, latency=0.5, cost=0.5, compliance=0)


class TestStatistics:
    def test_wilson_interval(self) -> None:
        low, high = wilson_interval(8, 10)
        assert low == pytest.approx(0.4902, abs=1e-3)
        assert high == pytest.approx(0.9433, abs=1e-3)
        assert wilson_interval(0, 0) == (0.0, 1.0)
        assert wilson_interval(10, 10)[1] == pytest.approx(1.0)

    def test_percentile(self) -> None:
        values = [10.0, 20.0, 30.0, 40.0]
        assert percentile(values, 50) == pytest.approx(25.0)
        assert percentile(values, 0) == 10.0
        assert percentile(values, 100) == 40.0
        assert percentile([], 95) == 0.0
        with pytest.raises(ValueError, match="q must be"):
            percentile(values, 101)

    def test_weighted_kappa(self) -> None:
        assert quadratic_weighted_kappa([1, 2, 3, 4, 5], [1, 2, 3, 4, 5]) == pytest.approx(1.0)
        assert quadratic_weighted_kappa([1, 5, 1, 5], [5, 1, 5, 1]) < 0
        assert quadratic_weighted_kappa([3, 3], [3, 3]) == 1.0
        with pytest.raises(ValueError, match="same items"):
            quadratic_weighted_kappa([1], [1, 2])
        with pytest.raises(ValueError, match="no ratings"):
            quadratic_weighted_kappa([], [])
        with pytest.raises(ValueError, match="within"):
            quadratic_weighted_kappa([0], [1])

    def test_spearman(self) -> None:
        assert spearman([1, 2, 3, 4], [10, 20, 30, 40]) == pytest.approx(1.0)
        assert spearman([1, 2, 3, 4], [4, 3, 2, 1]) == pytest.approx(-1.0)
        assert spearman([1, 1, 2], [3, 3, 4]) == pytest.approx(1.0)
        assert spearman([1, 1, 1], [1, 2, 3]) == 0.0
        assert spearman([1], [1]) == 0.0
        with pytest.raises(ValueError, match="equal length"):
            spearman([1, 2], [1])

    def test_isotonic_fit_pools_violators(self) -> None:
        points = isotonic_fit([1, 2, 3, 4], [1.0, 3.0, 2.0, 4.0])
        assert points == [(1, 1.0), (2, 2.5), (3, 2.5), (4, 4.0)]
        assert isotonic_predict(points, 0) == 1.0
        assert isotonic_predict(points, 5) == 4.0
        assert isotonic_predict(points, 3.5) == pytest.approx(3.25)
        with pytest.raises(ValueError, match="empty"):
            isotonic_predict([], 1)
        with pytest.raises(ValueError, match="no data"):
            isotonic_fit([], [])
        with pytest.raises(ValueError, match="equal length"):
            isotonic_fit([1], [])

    @settings(max_examples=60, deadline=None)
    @given(
        st.lists(
            st.tuples(st.integers(1, 5), st.floats(1, 5, allow_nan=False)), min_size=1, max_size=40
        )
    )
    def test_isotonic_output_is_monotone(self, pairs: list[tuple[int, float]]) -> None:
        points = isotonic_fit([float(x) for x, _ in pairs], [y for _, y in pairs])
        ys = [y for _, y in points]
        assert all(a <= b + 1e-12 for a, b in itertools.pairwise(ys))


class TestBradleyTerry:
    def test_recovers_order(self) -> None:
        comparisons = [
            bradley_terry.Comparison(f"c{i}", "strong", "weak", 1.0) for i in range(8)
        ] + [
            bradley_terry.Comparison(f"c{i}", "strong", "mid", 1.0 if i < 6 else 0.0)
            for i in range(8)
        ]
        comparisons += [bradley_terry.Comparison(f"c{i}", "mid", "weak", 1.0) for i in range(6)]
        comparisons += [bradley_terry.Comparison("c9", "mid", "weak", 0.5)]
        ranking = bradley_terry.rank(
            ["strong", "mid", "weak"], comparisons, rng=random.Random(2), bootstrap_rounds=50
        )
        assert ranking.ordered() == ["strong", "mid", "weak"]
        assert sum(ranking.strengths.values()) == pytest.approx(1.0)
        low, high = ranking.intervals["strong"]
        assert low <= ranking.strengths["strong"] <= high
        assert ranking.wins["strong"] == 14
        assert ranking.comparisons["weak"] == 15

    def test_winless_player_keeps_positive_strength(self) -> None:
        strengths = bradley_terry.fit(
            ["a", "b"], [bradley_terry.Comparison("x", "a", "b", 1.0)] * 5
        )
        assert strengths["b"] > 0
        assert strengths["a"] > strengths["b"]

    def test_without_bootstrap_interval_is_point(self) -> None:
        ranking = bradley_terry.rank(
            ["a", "b"],
            [bradley_terry.Comparison("x", "a", "b", 0.5)],
            rng=random.Random(1),
            bootstrap_rounds=0,
        )
        assert ranking.intervals["a"] == (ranking.strengths["a"], ranking.strengths["a"])

    def test_validation(self) -> None:
        with pytest.raises(ValueError, match="itself"):
            bradley_terry.Comparison("x", "a", "a", 1.0)
        with pytest.raises(ValueError, match="result"):
            bradley_terry.Comparison("x", "a", "b", 0.7)
        with pytest.raises(ValueError, match="two players"):
            bradley_terry.fit(["a"], [])
        with pytest.raises(ValueError, match="unknown player"):
            bradley_terry.fit(["a", "b"], [bradley_terry.Comparison("x", "a", "c", 1.0)])
