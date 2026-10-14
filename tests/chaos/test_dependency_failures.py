"""Fault injection: providers, judges, database and audit storage failing mid-flight."""

from __future__ import annotations

from typing import Any

import pytest
from agentic_core.models.resilience import BreakerState
from fastapi.testclient import TestClient

from model_routing_evaluation.application.commands import RouteCommand, RunCommand
from model_routing_evaluation.domain.errors import UpstreamUnavailableError
from model_routing_evaluation.domain.evaluation import RunStatus
from model_routing_evaluation.domain.models import ShadowOutcome
from model_routing_evaluation.infrastructure.database import make_engine
from model_routing_evaluation.runtime.container import Container
from model_routing_evaluation.security.identity import Caller
from tests.conftest import AGENT, FakeIdp, FakeModelWorld, agent_headers, route_body

pytestmark = pytest.mark.chaos

CALLER = Caller(subject=AGENT, kind="agent")


def command(**kwargs: Any) -> RouteCommand:
    data: dict[str, Any] = {
        "task": "general",
        "messages": [{"role": "user", "content": "Draft the change announcement."}],
        "strategy": "priority",
    }
    data.update(kwargs)
    return RouteCommand.model_validate(data)


def test_database_outage_flips_readiness(client: TestClient) -> None:
    container = client.app.state.container  # type: ignore[attr-defined]
    assert client.get("/readyz").status_code == 200
    healthy = container.engine
    container.engine = make_engine("sqlite+aiosqlite:////nonexistent/dir/db.sqlite")
    try:
        assert client.get("/readyz").status_code == 503
        assert client.get("/healthz").status_code == 200
    finally:
        container.engine = healthy


async def test_every_provider_down_returns_503_and_learns(
    container: Container, world: FakeModelWorld
) -> None:
    world.down.update({"azure-gpt", "claude", "llama"})
    with pytest.raises(UpstreamUnavailableError) as info:
        await container.routing.route(command(), CALLER)
    assert len(info.value.details["attempts"]) == 6  # two tries per provider
    stats = await container.routing._bandit.load("general")  # type: ignore[attr-defined]
    assert {p for (bucket, p), s in stats.items() if bucket == "*" and s.quality_n} == {
        "azure-gpt",
        "claude",
        "llama",
    }


async def test_circuit_breaker_skips_failing_provider(
    container: Container, world: FakeModelWorld
) -> None:
    world.down.add("azure-gpt")
    for _ in range(3):
        result = await container.routing.route(command(), CALLER)
        assert result.provider == "claude"
    assert container.routing.router.breaker("azure-gpt").state is BreakerState.OPEN
    calls_before = world.calls["claude"]
    result = await container.routing.route(command(), CALLER)
    assert result.attempts[0].outcome.value == "skipped_circuit_open"
    assert world.calls["claude"] == calls_before + 1


async def test_bedrock_throttling_fails_over(container: Container, world: FakeModelWorld) -> None:
    world.down.add("nova")
    family = container.routing.config.families["classify"]
    container.routing.config.families["classify"] = family.model_copy(
        update={"pool": ("nova", "phi")}
    )
    result = await container.routing.route(command(task="classify"), CALLER)
    assert result.provider == "phi"
    assert result.attempts[0].error is not None
    assert "ThrottlingException" in result.attempts[0].error


async def test_judge_outage_marks_cases_but_run_completes(
    container: Container, world: FakeModelWorld
) -> None:
    world.down.update({"claude", "azure-gpt"})  # both summary judges
    run = await container.evaluation.start_run(
        RunCommand(golden_set="incident-summary", providers=("phi",)), CALLER
    )
    await container.evaluation.drain()
    finished, results = await container.evaluation.get_run(run.run_id)
    assert finished.status is RunStatus.COMPLETED
    assert all(r.error is not None and r.error.startswith("judge") for r in results)


async def test_shadow_failure_never_affects_primary(
    container: Container, world: FakeModelWorld
) -> None:
    family = container.routing.config.families["summarize"]
    container.routing.config.families["summarize"] = family.model_copy(
        update={"shadow": family.shadow.model_copy(update={"rate": 1.0, "providers": ("nova",)})}
    )
    world.down.add("nova")
    result = await container.routing.route(command(task="summarize"), CALLER)
    assert result.provider == "claude"
    await container.routing.drain()
    shadows = await container.routing._shadows.list_since(  # type: ignore[attr-defined]
        "summarize", container.clock.now().replace(year=2000)
    )
    assert [s.outcome for s in shadows] == [ShadowOutcome.ERROR]


async def test_shadow_crash_is_contained(container: Container, world: FakeModelWorld) -> None:
    family = container.routing.config.families["general"]
    container.routing.config.families["general"] = family.model_copy(
        update={"shadow": family.shadow.model_copy(update={"rate": 1.0})}
    )

    async def broken(_: object) -> None:
        raise RuntimeError("shadow store unavailable")

    container.routing._shadows.add = broken  # type: ignore[method-assign]
    result = await container.routing.route(command(), CALLER)
    await container.routing.drain()
    assert result.provider == "azure-gpt"


async def test_audit_failure_blocks_routing(container: Container) -> None:
    """If evidence cannot be written, the decision must not complete silently."""

    async def broken_append(_: object) -> None:
        raise OSError("disk full")

    container.audit._sink.append = broken_append  # type: ignore[method-assign]
    with pytest.raises(OSError, match="disk full"):
        await container.routing.route(command(), CALLER)


async def test_interrupted_runs_are_failed_on_restart(container: Container) -> None:
    import asyncio

    started = asyncio.Event()
    release = asyncio.Event()

    async def slow(_: Any) -> Any:
        started.set()
        await release.wait()
        raise asyncio.CancelledError

    container.evaluation._evaluator.run = slow  # type: ignore[method-assign]
    run = await container.evaluation.start_run(
        RunCommand(golden_set="support-ticket-triage", providers=("phi",)), CALLER
    )
    await started.wait()
    assert await container.evaluation.recover() == 1
    finished, _ = await container.evaluation.get_run(run.run_id)
    assert finished.status is RunStatus.FAILED
    assert finished.error == "interrupted by service restart"
    release.set()
    for task in list(container.evaluation._tasks):  # type: ignore[attr-defined]
        task.cancel()
    await container.evaluation.drain()


def test_rate_of_unauthenticated_calls_does_not_reach_models(
    client: TestClient, world: FakeModelWorld, agent_idp: FakeIdp
) -> None:
    for _ in range(20):
        assert client.post("/v1/route", json=route_body()).status_code == 401
    assert sum(world.calls.values()) == 0
    assert (
        client.post("/v1/route", json=route_body(), headers=agent_headers(agent_idp)).status_code
        == 200
    )
