"""Response bodies specific to the HTTP API (commands and results live in application.commands)."""

from __future__ import annotations

from datetime import datetime

from agentic_core.domain.classification import DataClassification
from agentic_core.models.resilience import BreakerState
from pydantic import BaseModel, ConfigDict

from model_routing_evaluation.domain.evaluation import CaseResult, EvaluationRun
from model_routing_evaluation.domain.models import ComplianceProfile, LedgerTotal


class ProviderView(BaseModel):
    model_config = ConfigDict(frozen=True)

    name: str
    model: str
    tasks: list[str]
    max_classification: DataClassification
    region: str | None
    compliance: ComplianceProfile | None
    circuit: BreakerState


class ProviderList(BaseModel):
    items: list[ProviderView]


class GoldenSetView(BaseModel):
    model_config = ConfigDict(frozen=True)

    name: str
    version: str
    task: str
    description: str
    classification: DataClassification
    cases: int
    grader: str
    uses_judge: bool
    digest: str


class GoldenSetList(BaseModel):
    items: list[GoldenSetView]


class EvaluationRunView(BaseModel):
    run: EvaluationRun
    results: list[CaseResult]


class LedgerView(BaseModel):
    since: datetime
    until: datetime
    group_by: str
    family: str | None
    totals: list[LedgerTotal]
    total_cost_usd: float


class ChainVerification(BaseModel):
    chain_id: str
    events: int
    intact: bool
    error: str | None = None


class AuditVerification(BaseModel):
    key_id: str
    chains: list[ChainVerification]


class OAuthError(BaseModel):
    error: str
    error_description: str
