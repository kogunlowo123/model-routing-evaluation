"""SQLAlchemy repositories against SQLite (PostgreSQL runs the same code in CI)."""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from agentic_core.audit import AuditLog
from agentic_core.domain.classification import DataClassification
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncEngine

from model_routing_evaluation.domain.evaluation import (
    CaseResult,
    EvaluationRun,
    Interval,
    Promotion,
    PromotionAction,
    PromotionStatus,
    ProviderSummary,
    RunStatus,
    RunSummary,
)
from model_routing_evaluation.domain.models import (
    DecisionRecord,
    LedgerEntry,
    LedgerGroup,
    LedgerKind,
    ShadowOutcome,
    ShadowResult,
    Strategy,
)
from model_routing_evaluation.domain.stats import FAMILY_BUCKET, Observation
from model_routing_evaluation.infrastructure.database import (
    SqlAuditSink,
    SqlBanditStore,
    SqlDecisionRepository,
    SqlEvaluationRepository,
    SqlLedger,
    SqlPromotionRepository,
    SqlShadowRepository,
    chain_ids,
    create_schema,
    make_engine,
)

NOW = datetime(2026, 10, 14, 12, 0, tzinfo=UTC)


@pytest.fixture
async def engine(tmp_path: Path) -> AsyncIterator[AsyncEngine]:
    engine = make_engine(f"sqlite+aiosqlite:///{tmp_path / 'p.db'}")
    await create_schema(engine)
    await create_schema(engine)  # idempotent
    yield engine
    await engine.dispose()


def decision(decision_id: str = "d1", provider: str = "phi") -> DecisionRecord:
    return DecisionRecord(
        decision_id=decision_id,
        family="classify",
        bucket="short",
        strategy=Strategy.LEARNED,
        caller="agent-a",
        classification=DataClassification.INTERNAL,
        provider=provider,
        model="Phi-4",
        candidates=("phi", "azure-gpt"),
        excluded=(),
        attempts=("phi#1:success",),
        input_digest="sha256:" + "1" * 64,
        output_digest="sha256:" + "2" * 64,
        input_tokens=10,
        output_tokens=2,
        cost_usd=0.001,
        latency_ms=120.0,
        quality=0.5,
        quality_source="schema",
        created_at=NOW,
    )


async def test_decisions_and_feedback_once(engine: AsyncEngine) -> None:
    repo = SqlDecisionRepository(engine)
    await repo.add(decision())
    assert (await repo.get("d1")) == decision()
    assert await repo.get("missing") is None
    assert await repo.set_quality("d1", 0.9, "feedback")  # replaces the schema grade
    assert not await repo.set_quality("d1", 0.1, "feedback")  # feedback is recorded once
    stored = await repo.get("d1")
    assert stored is not None
    assert stored.quality == 0.9
    assert stored.quality_source == "feedback"
    rows = await repo.traffic("classify", NOW - timedelta(days=1))
    assert [(r.provider, r.quality) for r in rows] == [("phi", 0.9)]
    assert await repo.traffic("classify", NOW + timedelta(days=1)) == []


async def test_ledger_totals_and_spend(engine: AsyncEngine) -> None:
    ledger = SqlLedger(engine)
    for kind, provider, cost in (
        (LedgerKind.ROUTE, "phi", 0.01),
        (LedgerKind.ROUTE, "claude", 0.02),
        (LedgerKind.SHADOW, "nova", 0.005),
    ):
        await ledger.record(
            LedgerEntry(
                reference="r",
                kind=kind,
                family="classify",
                provider=provider,
                model="m",
                caller="agent-a",
                input_tokens=100,
                output_tokens=10,
                cost_usd=cost,
                created_at=NOW,
            )
        )
    window = {"since": NOW - timedelta(hours=1), "until": NOW + timedelta(hours=1)}
    by_kind = await ledger.totals(group_by=LedgerGroup.KIND, **window)
    assert [(t.key, t.entries) for t in by_kind] == [("route", 2), ("shadow", 1)]
    assert by_kind[0].cost_usd == pytest.approx(0.03)
    assert await ledger.totals(group_by=LedgerGroup.PROVIDER, family="other", **window) == []
    assert await ledger.spent(kind="shadow", since=NOW - timedelta(hours=1)) == pytest.approx(0.005)
    assert await ledger.spent(kind="shadow", since=NOW + timedelta(hours=1)) == 0.0


async def test_bandit_statistics_accumulate_in_both_buckets(engine: AsyncEngine) -> None:
    store = SqlBanditStore(engine)
    await store.observe("classify", "short", "phi", Observation(quality=1.0, latency_ms=100))
    await store.observe("classify", "short", "phi", Observation(quality=0.0))
    await store.observe("classify", FAMILY_BUCKET, "phi", Observation(cost_usd=0.5))
    stats = await store.load("classify")
    assert stats[("short", "phi")].quality_n == 2
    assert stats[("short", "phi")].quality_sum == 1
    assert stats[("*", "phi")].quality_n == 2
    assert stats[("*", "phi")].cost_n == 1
    assert ("short", "phi") in stats
    assert len(stats) == 2
    assert await store.load("extract") == {}


async def test_evaluation_runs_results_and_recovery(engine: AsyncEngine) -> None:
    repo = SqlEvaluationRepository(engine)
    run = EvaluationRun(
        run_id="run-1",
        golden_set="support-ticket-triage",
        golden_version="1",
        golden_digest="sha256:" + "3" * 64,
        family="classify",
        providers=("phi",),
        pairwise=False,
        status=RunStatus.RUNNING,
        requested_by="agent-a",
        estimated_cost_usd=0.01,
        created_at=NOW,
    )
    await repo.add_run(run)
    await repo.add_run(run.model_copy(update={"run_id": "run-2", "created_at": NOW + timedelta(1)}))
    await repo.add_results("run-1", [])
    await repo.add_results(
        "run-1",
        [
            CaseResult(
                case_id="tt-001", provider="phi", score=1, passed=True, latency_ms=1, cost_usd=0
            )
        ],
    )
    summary = RunSummary(
        providers=(
            ProviderSummary(
                provider="phi",
                cases=1,
                errors=0,
                pass_rate=1.0,
                pass_rate_ci95=Interval(low=0.2, high=1.0),
                mean_score=1.0,
                latency_p50_ms=1,
                latency_p95_ms=1,
                total_cost_usd=0,
                cost_per_pass_usd=0,
            ),
        )
    )
    await repo.finish_run("run-1", status=RunStatus.COMPLETED, summary=summary, error=None, at=NOW)
    await repo.finish_run("missing", status=RunStatus.FAILED, summary=None, error="x", at=NOW)
    finished = await repo.get_run("run-1")
    assert finished is not None
    assert finished.status is RunStatus.COMPLETED
    assert [r.case_id for r in await repo.results("run-1")] == ["tt-001"]
    assert (await repo.latest_completed("classify")) == finished
    assert await repo.latest_completed("extract") is None
    assert await repo.get_run("nope") is None
    assert await repo.fail_running(reason="restart", at=NOW) == 1
    interrupted = await repo.get_run("run-2")
    assert interrupted is not None
    assert interrupted.error == "restart"


async def test_promotions_decided_exactly_once(engine: AsyncEngine) -> None:
    repo = SqlPromotionRepository(engine)
    promotion = Promotion(
        promotion_id="p1",
        family="classify",
        provider="llama",
        action=PromotionAction.ADD,
        run_id="run-1",
        justification="passed the gate in run-1",
        status=PromotionStatus.PENDING,
        requested_by="agent-a",
        evidence={"pass_rate": 1.0},
        policy_version="v",
        policy_digest="d",
        created_at=NOW,
    )
    await repo.add(promotion)
    assert await repo.decide(
        "p1", status=PromotionStatus.APPROVED, decided_by="dana", reason="ok", at=NOW
    )
    assert not await repo.decide(
        "p1", status=PromotionStatus.REJECTED, decided_by="eve", reason="late", at=NOW
    )
    assert not await repo.decide(
        "missing", status=PromotionStatus.APPROVED, decided_by="d", reason="r", at=NOW
    )
    stored = await repo.get("p1")
    assert stored is not None
    assert stored.decided_by == "dana"
    assert [p.promotion_id for p in await repo.list(family="classify")] == ["p1"]
    assert await repo.list(status=PromotionStatus.PENDING) == []
    assert await repo.get("missing") is None


async def test_shadow_results(engine: AsyncEngine) -> None:
    repo = SqlShadowRepository(engine)
    result = ShadowResult(
        shadow_id="s1",
        decision_id="d1",
        family="summarize",
        primary_provider="claude",
        shadow_provider="nova",
        outcome=ShadowOutcome.WIN,
        shadow_quality=1.0,
        latency_ms=10,
        cost_usd=0.001,
        detail="judge=azure-gpt",
        created_at=NOW,
    )
    await repo.add(result)
    assert await repo.list_since("summarize", NOW - timedelta(minutes=1)) == [result]


async def test_audit_chain_is_append_only(engine: AsyncEngine) -> None:
    key = Ed25519PrivateKey.generate()
    sink = SqlAuditSink(engine, "replica-a")
    log = AuditLog(sink, key, key_id="k")
    await log.record(actor="a", action="route.completed", resource="decision:1", outcome="served")
    await log.record(actor="a", action="route.feedback", resource="decision:1", outcome="recorded")
    assert (await sink.tail()) is not None
    assert [e.sequence async for e in sink.read()] == [0, 1]
    assert await chain_ids(engine) == ["replica-a"]
    assert await SqlAuditSink(engine, "empty").tail() is None
    for statement in ("UPDATE audit_events SET document = 'x'", "DELETE FROM audit_events"):
        with pytest.raises(DBAPIError, match="append-only"):
            async with engine.begin() as conn:
                await conn.execute(text(statement))
