"""Latency budgets for hot paths. Loose enough for shared CI runners."""

from __future__ import annotations

import math
import random
import time

import pytest

from model_routing_evaluation.application.commands import RouteCommand
from model_routing_evaluation.domain.stats import ArmStats
from model_routing_evaluation.evaluation import bradley_terry
from model_routing_evaluation.routing.bandit import ThompsonSampler
from model_routing_evaluation.runtime.container import Container
from model_routing_evaluation.security.identity import Caller
from tests.conftest import AGENT

pytestmark = pytest.mark.performance


def test_thompson_sampling_budget() -> None:
    sampler = ThompsonSampler(random.Random(1))
    stats = ArmStats(
        quality_n=500,
        quality_sum=410,
        latency_n=500,
        latency_log_sum=500 * math.log(800),
        latency_log_sumsq=500 * math.log(800) ** 2 + 40,
        cost_n=500,
        cost_sum=4.0,
    )
    started = time.perf_counter()
    for _ in range(6000):  # 1000 decisions over six providers
        sampler.sample(stats, latency_target_ms=1000, estimated_cost_usd=0.01)
    per_decision_ms = (time.perf_counter() - started) * 1000 / 1000
    assert per_decision_ms < 1.0


def test_bradley_terry_budget() -> None:
    players = ["azure-gpt", "claude", "gemini", "nova", "llama", "phi"]
    rng = random.Random(4)
    comparisons = [
        bradley_terry.Comparison(f"case-{i}", a, b, rng.choice([0.0, 0.5, 1.0]))
        for i in range(50)
        for j, a in enumerate(players)
        for b in players[j + 1 :]
    ]
    started = time.perf_counter()
    bradley_terry.rank(players, comparisons, rng=rng, bootstrap_rounds=50)
    assert time.perf_counter() - started < 10.0


async def test_routing_overhead_budget(container: Container) -> None:
    """Planning, filtering, guarding, persistence and audit around a mocked model call."""
    caller = Caller(subject=AGENT, kind="agent")
    command = RouteCommand.model_validate(
        {"task": "general", "messages": [{"role": "user", "content": "Plan the rollout."}]}
    )
    await container.routing.route(command, caller)  # warm-up
    started = time.perf_counter()
    for _ in range(20):
        await container.routing.route(command, caller)
    per_request_ms = (time.perf_counter() - started) * 1000 / 20
    assert per_request_ms < 250
