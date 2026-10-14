"""Repository behaviour against a real PostgreSQL (CI service container).

Runs when MRE_TEST_DATABASE_URL points at PostgreSQL, e.g.
postgresql+asyncpg://mre:mre@localhost:5432/mre
"""

from __future__ import annotations

import asyncio
import os
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from agentic_core.audit import AuditLog, verify_chain
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from model_routing_evaluation.domain.models import LedgerEntry, LedgerGroup, LedgerKind
from model_routing_evaluation.domain.stats import Observation
from model_routing_evaluation.infrastructure.database import (
    SqlAuditSink,
    SqlBanditStore,
    SqlDecisionRepository,
    SqlLedger,
    create_schema,
    make_engine,
)
from tests.unit.test_persistence import decision

URL = os.environ.get("MRE_TEST_DATABASE_URL", "")

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not URL.startswith("postgresql"), reason="PostgreSQL not configured"),
]


async def test_schema_is_idempotent_under_concurrent_startup() -> None:
    engines = [make_engine(URL) for _ in range(3)]
    await asyncio.gather(*(create_schema(e) for e in engines))
    for engine in engines:
        await engine.dispose()


async def test_concurrent_bandit_updates_are_not_lost() -> None:
    engine = make_engine(URL)
    await create_schema(engine)
    store = SqlBanditStore(engine)
    family = f"pg-{uuid4().hex[:8]}"
    await asyncio.gather(
        *(store.observe(family, "short", "phi", Observation(quality=1.0)) for _ in range(20))
    )
    stats = await store.load(family)
    assert stats[("short", "phi")].quality_n == 20
    assert stats[("*", "phi")].quality_sum == 20
    await engine.dispose()


async def test_repositories_round_trip() -> None:
    engine = make_engine(URL)
    await create_schema(engine)
    repo = SqlDecisionRepository(engine)
    record = decision(decision_id=uuid4().hex)
    await repo.add(record)
    assert await repo.get(record.decision_id) == record
    assert await repo.set_quality(record.decision_id, 1.0, "feedback")
    ledger = SqlLedger(engine)
    now = datetime.now(UTC)
    await ledger.record(
        LedgerEntry(
            reference=record.decision_id,
            kind=LedgerKind.ROUTE,
            family="pg-family",
            provider="phi",
            model="Phi-4",
            caller="agent",
            input_tokens=10,
            output_tokens=5,
            cost_usd=0.25,
            created_at=now,
        )
    )
    totals = await ledger.totals(
        since=now - timedelta(minutes=1),
        until=now + timedelta(minutes=1),
        group_by=LedgerGroup.FAMILY,
        family="pg-family",
    )
    assert totals[0].cost_usd >= 0.25
    await engine.dispose()


async def test_audit_table_rejects_updates_and_deletes() -> None:
    engine = make_engine(URL)
    await create_schema(engine)
    key = Ed25519PrivateKey.generate()
    sink = SqlAuditSink(engine, f"pg-{uuid4().hex[:8]}")
    log = AuditLog(sink, key, key_id="k")
    for i in range(3):
        await log.record(actor="a", action="route.completed", resource=f"d{i}", outcome="served")
    events = [e async for e in sink.read()]
    assert verify_chain(events, {"k": key.public_key()}) == 3
    for statement in ("UPDATE audit_events SET document = 'x'", "DELETE FROM audit_events"):
        with pytest.raises(DBAPIError, match="append-only"):
            async with engine.begin() as conn:
                await conn.execute(text(statement))
    await engine.dispose()
