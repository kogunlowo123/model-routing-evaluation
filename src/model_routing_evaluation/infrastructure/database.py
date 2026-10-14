"""SQLAlchemy Core persistence. PostgreSQL in production; SQLite through the same code in tests."""

from __future__ import annotations

from collections.abc import AsyncIterator, Sequence
from datetime import UTC, datetime
from typing import Any

from agentic_core.audit import AuditEvent
from sqlalchemy import (
    Boolean,
    Column,
    DateTime,
    Float,
    Index,
    Integer,
    MetaData,
    String,
    Table,
    Text,
    and_,
    func,
    insert,
    or_,
    select,
    text,
    update,
)
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine, create_async_engine

from model_routing_evaluation.domain.errors import ConflictError
from model_routing_evaluation.domain.evaluation import (
    CalibrationResult,
    CaseResult,
    EvaluationRun,
    Promotion,
    PromotionStatus,
    RunStatus,
    RunSummary,
)
from model_routing_evaluation.domain.models import (
    DecisionRecord,
    LedgerEntry,
    LedgerGroup,
    LedgerTotal,
    ShadowResult,
    TrafficRow,
)
from model_routing_evaluation.domain.stats import FAMILY_BUCKET, STAT_FIELDS, ArmStats, Observation

metadata = MetaData()

routing_decisions = Table(
    "routing_decisions",
    metadata,
    Column("decision_id", String(64), primary_key=True),
    Column("family", String(64), nullable=False),
    Column("provider", String(128), nullable=False),
    Column("caller", String(512), nullable=False),
    Column("latency_ms", Float, nullable=False),
    Column("cost_usd", Float, nullable=False),
    Column("quality", Float, nullable=True),
    Column("quality_source", String(32), nullable=True),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("document", Text, nullable=False),
    Index("ix_decisions_family_time", "family", "created_at"),
)

cost_ledger = Table(
    "cost_ledger",
    metadata,
    Column("entry_id", Integer, primary_key=True, autoincrement=True),
    Column("reference", String(64), nullable=False),
    Column("kind", String(16), nullable=False),
    Column("family", String(64), nullable=False),
    Column("provider", String(128), nullable=False),
    Column("model", String(256), nullable=False),
    Column("caller", String(512), nullable=False),
    Column("input_tokens", Integer, nullable=False),
    Column("output_tokens", Integer, nullable=False),
    Column("cost_usd", Float, nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Index("ix_ledger_time", "created_at"),
    Index("ix_ledger_kind_time", "kind", "created_at"),
)

bandit_stats = Table(
    "bandit_stats",
    metadata,
    Column("family", String(64), primary_key=True),
    Column("bucket", String(16), primary_key=True),
    Column("provider", String(128), primary_key=True),
    *(Column(name, Float, nullable=False, default=0.0) for name in STAT_FIELDS),
    Column("updated_at", DateTime(timezone=True), nullable=False),
)

evaluation_runs = Table(
    "evaluation_runs",
    metadata,
    Column("run_id", String(64), primary_key=True),
    Column("family", String(64), nullable=False),
    Column("status", String(16), nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("document", Text, nullable=False),
    Index("ix_runs_family_status", "family", "status", "created_at"),
)

evaluation_results = Table(
    "evaluation_results",
    metadata,
    Column("run_id", String(64), primary_key=True),
    Column("case_id", String(64), primary_key=True),
    Column("provider", String(128), primary_key=True),
    Column("document", Text, nullable=False),
)

judge_calibrations = Table(
    "judge_calibrations",
    metadata,
    Column("calibration_id", String(64), primary_key=True),
    Column("judge_provider", String(128), nullable=False),
    Column("rubric_digest", String(80), nullable=False),
    Column("calibrated", Boolean, nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("document", Text, nullable=False),
    Index("ix_calibrations_judge", "judge_provider", "rubric_digest", "created_at"),
)

promotions = Table(
    "promotions",
    metadata,
    Column("promotion_id", String(64), primary_key=True),
    Column("family", String(64), nullable=False),
    Column("status", String(16), nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("document", Text, nullable=False),
    Index("ix_promotions_family_status", "family", "status"),
)

shadow_results = Table(
    "shadow_results",
    metadata,
    Column("shadow_id", String(64), primary_key=True),
    Column("family", String(64), nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("document", Text, nullable=False),
    Index("ix_shadow_family_time", "family", "created_at"),
)

audit_events = Table(
    "audit_events",
    metadata,
    Column("chain_id", String(128), primary_key=True),
    Column("sequence", Integer, primary_key=True, autoincrement=False),
    Column("document", Text, nullable=False),
)

schema_version = Table("schema_version", metadata, Column("version", Integer, primary_key=True))

SCHEMA_VERSION = 1
_SCHEMA_LOCK_ID = 7_351_002  # pg advisory lock key serialising concurrent start-ups

_POSTGRES_APPEND_ONLY = (
    """
    CREATE OR REPLACE FUNCTION mre_audit_append_only() RETURNS trigger
    LANGUAGE plpgsql AS $$
    BEGIN
      RAISE EXCEPTION 'audit_events is append-only';
    END;
    $$
    """,
    "DROP TRIGGER IF EXISTS audit_events_append_only ON audit_events",
    """
    CREATE TRIGGER audit_events_append_only BEFORE UPDATE OR DELETE ON audit_events
    FOR EACH ROW EXECUTE FUNCTION mre_audit_append_only()
    """,
)

_SQLITE_APPEND_ONLY = (
    """
    CREATE TRIGGER IF NOT EXISTS audit_events_no_update BEFORE UPDATE ON audit_events
    BEGIN SELECT RAISE(ABORT, 'audit_events is append-only'); END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS audit_events_no_delete BEFORE DELETE ON audit_events
    BEGIN SELECT RAISE(ABORT, 'audit_events is append-only'); END
    """,
)


def make_engine(url: str) -> AsyncEngine:
    return create_async_engine(url, pool_pre_ping=True)


async def create_schema(engine: AsyncEngine) -> None:
    async with engine.begin() as conn:
        dialect = conn.dialect.name
        if dialect == "postgresql":
            await conn.execute(select(func.pg_advisory_xact_lock(_SCHEMA_LOCK_ID)))
        await conn.run_sync(metadata.create_all)
        statements = _POSTGRES_APPEND_ONLY if dialect == "postgresql" else _SQLITE_APPEND_ONLY
        if dialect in {"postgresql", "sqlite"}:
            for statement in statements:
                await conn.execute(text(statement))
        current = (await conn.execute(select(schema_version.c.version))).scalar()
        if current is None:
            await conn.execute(insert(schema_version).values(version=SCHEMA_VERSION))


def aware(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=UTC)


class SqlDecisionRepository:
    def __init__(self, engine: AsyncEngine) -> None:
        self._engine = engine

    async def add(self, record: DecisionRecord) -> None:
        async with self._engine.begin() as conn:
            await conn.execute(
                insert(routing_decisions).values(
                    decision_id=record.decision_id,
                    family=record.family,
                    provider=record.provider,
                    caller=record.caller,
                    latency_ms=record.latency_ms,
                    cost_usd=record.cost_usd,
                    quality=record.quality,
                    quality_source=record.quality_source,
                    created_at=record.created_at,
                    document=record.model_dump_json(),
                )
            )

    async def get(self, decision_id: str) -> DecisionRecord | None:
        query = select(
            routing_decisions.c.document,
            routing_decisions.c.quality,
            routing_decisions.c.quality_source,
        ).where(routing_decisions.c.decision_id == decision_id)
        async with self._engine.connect() as conn:
            row = (await conn.execute(query)).first()
        if row is None:
            return None
        record = DecisionRecord.model_validate_json(row[0])
        return record.model_copy(update={"quality": row[1], "quality_source": row[2]})

    async def set_quality(self, decision_id: str, quality: float, source: str) -> bool:
        """Record caller feedback once per decision; it may replace an automatic schema grade.

        Returns False when feedback was already recorded for the decision.
        """
        async with self._engine.begin() as conn:
            result = await conn.execute(
                update(routing_decisions)
                .where(
                    and_(
                        routing_decisions.c.decision_id == decision_id,
                        or_(
                            routing_decisions.c.quality_source.is_(None),
                            routing_decisions.c.quality_source != source,
                        ),
                    )
                )
                .values(quality=quality, quality_source=source)
            )
        return bool(result.rowcount)

    async def traffic(self, family: str, since: datetime) -> Sequence[TrafficRow]:
        query = (
            select(
                routing_decisions.c.provider,
                routing_decisions.c.latency_ms,
                routing_decisions.c.cost_usd,
                routing_decisions.c.quality,
            )
            .where(
                and_(routing_decisions.c.family == family, routing_decisions.c.created_at >= since)
            )
            .order_by(routing_decisions.c.created_at.desc())
            .limit(100_000)
        )
        async with self._engine.connect() as conn:
            rows = (await conn.execute(query)).all()
        return [
            TrafficRow(provider=r[0], latency_ms=r[1], cost_usd=r[2], quality=r[3]) for r in rows
        ]


class SqlLedger:
    def __init__(self, engine: AsyncEngine) -> None:
        self._engine = engine

    async def record(self, entry: LedgerEntry) -> None:
        async with self._engine.begin() as conn:
            await conn.execute(insert(cost_ledger).values(**entry.model_dump(mode="python")))

    async def totals(
        self,
        *,
        since: datetime,
        until: datetime,
        group_by: LedgerGroup,
        family: str | None = None,
    ) -> Sequence[LedgerTotal]:
        key = cost_ledger.c[group_by.value]
        conditions = [cost_ledger.c.created_at >= since, cost_ledger.c.created_at < until]
        if family is not None:
            conditions.append(cost_ledger.c.family == family)
        query = (
            select(
                key,
                func.count(),
                func.coalesce(func.sum(cost_ledger.c.input_tokens), 0),
                func.coalesce(func.sum(cost_ledger.c.output_tokens), 0),
                func.coalesce(func.sum(cost_ledger.c.cost_usd), 0.0),
            )
            .where(and_(*conditions))
            .group_by(key)
            .order_by(key)
        )
        async with self._engine.connect() as conn:
            rows = (await conn.execute(query)).all()
        return [
            LedgerTotal(
                key=str(r[0]),
                entries=int(r[1]),
                input_tokens=int(r[2]),
                output_tokens=int(r[3]),
                cost_usd=float(r[4]),
            )
            for r in rows
        ]

    async def spent(self, *, kind: str, since: datetime) -> float:
        query = select(func.coalesce(func.sum(cost_ledger.c.cost_usd), 0.0)).where(
            and_(cost_ledger.c.kind == kind, cost_ledger.c.created_at >= since)
        )
        async with self._engine.connect() as conn:
            return float((await conn.execute(query)).scalar() or 0.0)


class SqlBanditStore:
    """Additive sufficient statistics. ``x = x + delta`` updates commute across replicas."""

    def __init__(self, engine: AsyncEngine) -> None:
        self._engine = engine

    async def load(self, family: str) -> dict[tuple[str, str], ArmStats]:
        columns = [bandit_stats.c.bucket, bandit_stats.c.provider] + [
            bandit_stats.c[name] for name in STAT_FIELDS
        ]
        async with self._engine.connect() as conn:
            rows = (
                await conn.execute(select(*columns).where(bandit_stats.c.family == family))
            ).all()
        return {
            (r[0], r[1]): ArmStats(**{n: float(v) for n, v in zip(STAT_FIELDS, r[2:], strict=True)})
            for r in rows
        }

    async def observe(self, family: str, bucket: str, provider: str, obs: Observation) -> None:
        delta = obs.as_delta()
        increments = {name: getattr(delta, name) for name in STAT_FIELDS}
        for target in {bucket, FAMILY_BUCKET}:
            await self._increment(family, target, provider, increments)

    async def _increment(
        self, family: str, bucket: str, provider: str, increments: dict[str, float]
    ) -> None:
        now = datetime.now(UTC)
        where = and_(
            bandit_stats.c.family == family,
            bandit_stats.c.bucket == bucket,
            bandit_stats.c.provider == provider,
        )
        values: dict[str, Any] = {n: bandit_stats.c[n] + v for n, v in increments.items()}
        values["updated_at"] = now
        for _ in range(2):
            async with self._engine.begin() as conn:
                result = await conn.execute(update(bandit_stats).where(where).values(**values))
                if result.rowcount:
                    return
            try:
                async with self._engine.begin() as conn:
                    await conn.execute(
                        insert(bandit_stats).values(
                            family=family,
                            bucket=bucket,
                            provider=provider,
                            updated_at=now,
                            **increments,
                        )
                    )
                return
            except IntegrityError:
                continue  # another replica inserted the row first; apply as an update
        raise ConflictError(f"could not update bandit statistics for {family}/{provider}")


class SqlEvaluationRepository:
    def __init__(self, engine: AsyncEngine) -> None:
        self._engine = engine

    async def add_run(self, run: EvaluationRun) -> None:
        async with self._engine.begin() as conn:
            await conn.execute(
                insert(evaluation_runs).values(
                    run_id=run.run_id,
                    family=run.family,
                    status=run.status.value,
                    created_at=run.created_at,
                    document=run.model_dump_json(),
                )
            )

    async def get_run(self, run_id: str) -> EvaluationRun | None:
        async with self._engine.connect() as conn:
            row = (
                await conn.execute(
                    select(evaluation_runs.c.document).where(evaluation_runs.c.run_id == run_id)
                )
            ).first()
        return EvaluationRun.model_validate_json(row[0]) if row else None

    async def finish_run(
        self,
        run_id: str,
        *,
        status: RunStatus,
        summary: RunSummary | None,
        error: str | None,
        at: datetime,
    ) -> None:
        async with self._engine.begin() as conn:
            await self._finish(conn, run_id, status=status, summary=summary, error=error, at=at)

    @staticmethod
    async def _finish(
        conn: AsyncConnection,
        run_id: str,
        *,
        status: RunStatus,
        summary: RunSummary | None,
        error: str | None,
        at: datetime,
    ) -> None:
        row = (
            await conn.execute(
                select(evaluation_runs.c.document).where(evaluation_runs.c.run_id == run_id)
            )
        ).first()
        if row is None:
            return
        run = EvaluationRun.model_validate_json(row[0]).model_copy(
            update={"status": status, "summary": summary, "error": error, "completed_at": at}
        )
        await conn.execute(
            update(evaluation_runs)
            .where(evaluation_runs.c.run_id == run_id)
            .values(status=status.value, document=run.model_dump_json())
        )

    async def add_results(self, run_id: str, results: Sequence[CaseResult]) -> None:
        if not results:
            return
        async with self._engine.begin() as conn:
            await conn.execute(
                insert(evaluation_results),
                [
                    {
                        "run_id": run_id,
                        "case_id": r.case_id,
                        "provider": r.provider,
                        "document": r.model_dump_json(),
                    }
                    for r in results
                ],
            )

    async def results(self, run_id: str) -> Sequence[CaseResult]:
        query = (
            select(evaluation_results.c.document)
            .where(evaluation_results.c.run_id == run_id)
            .order_by(evaluation_results.c.case_id, evaluation_results.c.provider)
        )
        async with self._engine.connect() as conn:
            rows = (await conn.execute(query)).all()
        return [CaseResult.model_validate_json(r[0]) for r in rows]

    async def latest_completed(self, family: str) -> EvaluationRun | None:
        query = (
            select(evaluation_runs.c.document)
            .where(
                and_(
                    evaluation_runs.c.family == family,
                    evaluation_runs.c.status == RunStatus.COMPLETED.value,
                )
            )
            .order_by(evaluation_runs.c.created_at.desc())
            .limit(1)
        )
        async with self._engine.connect() as conn:
            row = (await conn.execute(query)).first()
        return EvaluationRun.model_validate_json(row[0]) if row else None

    async def fail_running(self, *, reason: str, at: datetime) -> int:
        """Mark runs interrupted by a restart as failed. Returns how many were marked."""
        async with self._engine.begin() as conn:
            rows = (
                await conn.execute(
                    select(evaluation_runs.c.run_id).where(
                        evaluation_runs.c.status == RunStatus.RUNNING.value
                    )
                )
            ).all()
            for (run_id,) in rows:
                await self._finish(
                    conn, run_id, status=RunStatus.FAILED, summary=None, error=reason, at=at
                )
        return len(rows)

    async def add_calibration(self, result: CalibrationResult) -> None:
        async with self._engine.begin() as conn:
            await conn.execute(
                insert(judge_calibrations).values(
                    calibration_id=result.calibration_id,
                    judge_provider=result.judge_provider,
                    rubric_digest=result.rubric_digest,
                    calibrated=result.calibrated,
                    created_at=result.created_at,
                    document=result.model_dump_json(),
                )
            )

    async def latest_calibrations(self) -> dict[tuple[str, str], CalibrationResult]:
        query = select(judge_calibrations.c.document).order_by(judge_calibrations.c.created_at)
        async with self._engine.connect() as conn:
            rows = (await conn.execute(query)).all()
        latest: dict[tuple[str, str], CalibrationResult] = {}
        for (document,) in rows:
            result = CalibrationResult.model_validate_json(document)
            latest[(result.judge_provider, result.rubric_digest)] = result
        return latest


class SqlPromotionRepository:
    def __init__(self, engine: AsyncEngine) -> None:
        self._engine = engine

    async def add(self, promotion: Promotion) -> None:
        async with self._engine.begin() as conn:
            await conn.execute(
                insert(promotions).values(
                    promotion_id=promotion.promotion_id,
                    family=promotion.family,
                    status=promotion.status.value,
                    created_at=promotion.created_at,
                    document=promotion.model_dump_json(),
                )
            )

    async def get(self, promotion_id: str) -> Promotion | None:
        async with self._engine.connect() as conn:
            row = (
                await conn.execute(
                    select(promotions.c.document).where(promotions.c.promotion_id == promotion_id)
                )
            ).first()
        return Promotion.model_validate_json(row[0]) if row else None

    async def decide(
        self,
        promotion_id: str,
        *,
        status: PromotionStatus,
        decided_by: str,
        reason: str,
        at: datetime,
    ) -> bool:
        """Move a pending promotion to a decision exactly once; False if already decided."""
        async with self._engine.begin() as conn:
            row = (
                await conn.execute(
                    select(promotions.c.document).where(promotions.c.promotion_id == promotion_id)
                )
            ).first()
            if row is None:
                return False
            decided = Promotion.model_validate_json(row[0]).model_copy(
                update={
                    "status": status,
                    "decided_by": decided_by,
                    "decision_reason": reason,
                    "decided_at": at,
                }
            )
            result = await conn.execute(
                update(promotions)
                .where(
                    and_(
                        promotions.c.promotion_id == promotion_id,
                        promotions.c.status == PromotionStatus.PENDING.value,
                    )
                )
                .values(status=status.value, document=decided.model_dump_json())
            )
        return bool(result.rowcount)

    async def list(
        self, *, family: str | None = None, status: PromotionStatus | None = None
    ) -> Sequence[Promotion]:
        query = select(promotions.c.document).order_by(promotions.c.created_at)
        if family is not None:
            query = query.where(promotions.c.family == family)
        if status is not None:
            query = query.where(promotions.c.status == status.value)
        async with self._engine.connect() as conn:
            rows = (await conn.execute(query)).all()
        return [Promotion.model_validate_json(r[0]) for r in rows]


class SqlShadowRepository:
    def __init__(self, engine: AsyncEngine) -> None:
        self._engine = engine

    async def add(self, result: ShadowResult) -> None:
        async with self._engine.begin() as conn:
            await conn.execute(
                insert(shadow_results).values(
                    shadow_id=result.shadow_id,
                    family=result.family,
                    created_at=result.created_at,
                    document=result.model_dump_json(),
                )
            )

    async def list_since(self, family: str, since: datetime) -> Sequence[ShadowResult]:
        query = (
            select(shadow_results.c.document)
            .where(and_(shadow_results.c.family == family, shadow_results.c.created_at >= since))
            .order_by(shadow_results.c.created_at)
            .limit(100_000)
        )
        async with self._engine.connect() as conn:
            rows = (await conn.execute(query)).all()
        return [ShadowResult.model_validate_json(r[0]) for r in rows]


class SqlAuditSink:
    """agentic-core AuditSink storing one hash chain per replica in an append-only table."""

    def __init__(self, engine: AsyncEngine, chain_id: str) -> None:
        self._engine = engine
        self.chain_id = chain_id

    async def append(self, event: AuditEvent) -> None:
        async with self._engine.begin() as conn:
            await conn.execute(
                insert(audit_events).values(
                    chain_id=self.chain_id,
                    sequence=event.sequence,
                    document=event.model_dump_json(),
                )
            )

    async def read(self) -> AsyncIterator[AuditEvent]:
        query = (
            select(audit_events.c.document)
            .where(audit_events.c.chain_id == self.chain_id)
            .order_by(audit_events.c.sequence)
        )
        async with self._engine.connect() as conn:
            rows = (await conn.execute(query)).all()
        for (document,) in rows:
            yield AuditEvent.model_validate_json(document)

    async def tail(self) -> AuditEvent | None:
        query = (
            select(audit_events.c.document)
            .where(audit_events.c.chain_id == self.chain_id)
            .order_by(audit_events.c.sequence.desc())
            .limit(1)
        )
        async with self._engine.connect() as conn:
            row = (await conn.execute(query)).first()
        return AuditEvent.model_validate_json(row[0]) if row else None


async def chain_ids(engine: AsyncEngine) -> list[str]:
    async with engine.connect() as conn:
        rows = (await conn.execute(select(audit_events.c.chain_id).distinct())).all()
    return sorted(r[0] for r in rows)


class SystemClock:
    def now(self) -> datetime:
        return datetime.now(UTC)
