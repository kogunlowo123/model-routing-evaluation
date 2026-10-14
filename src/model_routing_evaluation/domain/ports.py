"""Ports the application layer depends on. Adapters live in infrastructure."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from typing import Protocol

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
from model_routing_evaluation.domain.stats import ArmStats, Observation


class Clock(Protocol):
    def now(self) -> datetime: ...


class BanditStore(Protocol):
    async def load(self, family: str) -> dict[tuple[str, str], ArmStats]: ...

    async def observe(self, family: str, bucket: str, provider: str, obs: Observation) -> None: ...


class DecisionRepository(Protocol):
    async def add(self, record: DecisionRecord) -> None: ...

    async def get(self, decision_id: str) -> DecisionRecord | None: ...

    async def set_quality(self, decision_id: str, quality: float, source: str) -> bool: ...

    async def traffic(self, family: str, since: datetime) -> Sequence[TrafficRow]: ...


class Ledger(Protocol):
    async def record(self, entry: LedgerEntry) -> None: ...

    async def totals(
        self,
        *,
        since: datetime,
        until: datetime,
        group_by: LedgerGroup,
        family: str | None = None,
    ) -> Sequence[LedgerTotal]: ...

    async def spent(self, *, kind: str, since: datetime) -> float: ...


class EvaluationRepository(Protocol):
    async def add_run(self, run: EvaluationRun) -> None: ...

    async def get_run(self, run_id: str) -> EvaluationRun | None: ...

    async def finish_run(
        self,
        run_id: str,
        *,
        status: RunStatus,
        summary: RunSummary | None,
        error: str | None,
        at: datetime,
    ) -> None: ...

    async def add_results(self, run_id: str, results: Sequence[CaseResult]) -> None: ...

    async def results(self, run_id: str) -> Sequence[CaseResult]: ...

    async def latest_completed(self, family: str) -> EvaluationRun | None: ...

    async def fail_running(self, *, reason: str, at: datetime) -> int: ...

    async def add_calibration(self, result: CalibrationResult) -> None: ...

    async def latest_calibrations(self) -> dict[tuple[str, str], CalibrationResult]: ...


class PromotionRepository(Protocol):
    async def add(self, promotion: Promotion) -> None: ...

    async def get(self, promotion_id: str) -> Promotion | None: ...

    async def decide(
        self,
        promotion_id: str,
        *,
        status: PromotionStatus,
        decided_by: str,
        reason: str,
        at: datetime,
    ) -> bool: ...

    async def list(
        self, *, family: str | None = None, status: PromotionStatus | None = None
    ) -> Sequence[Promotion]: ...


class ShadowRepository(Protocol):
    async def add(self, result: ShadowResult) -> None: ...

    async def list_since(self, family: str, since: datetime) -> Sequence[ShadowResult]: ...
