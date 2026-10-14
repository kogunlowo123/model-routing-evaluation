"""Routing domain: compliance facts, constraints, plans, decisions and ledger entries."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Annotated

from agentic_core.domain.classification import DataClassification
from pydantic import AfterValidator, BaseModel, ConfigDict, Field


def _jurisdiction(value: str) -> str:
    normalized = value.strip().upper()
    if not normalized.isalnum() or not 2 <= len(normalized) <= 8:
        raise ValueError(f"invalid jurisdiction code {value!r}")
    return normalized


Jurisdiction = Annotated[str, AfterValidator(_jurisdiction)]


class ComplianceProfile(BaseModel):
    """Contractual and residency facts about one provider deployment.

    These are deployment facts (which region the endpoint runs in, whether a
    BAA or DPA covers it), so they come from configuration, never code.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    jurisdiction: Jurisdiction = Field(description="Legal data residency, e.g. US, EU, UK")
    region: str = Field(min_length=1, max_length=64, description="Cloud region of the endpoint")
    baa: bool = Field(description="A HIPAA Business Associate Agreement covers this deployment")
    zero_data_retention: bool = Field(
        default=False, description="Prompts and outputs are not retained by the vendor"
    )

    @property
    def posture_score(self) -> float:
        """Soft compliance preference in [0, 1] used by the learned router's utility."""
        return 0.5 * float(self.baa) + 0.5 * float(self.zero_data_retention)


class RoutingConstraints(BaseModel):
    """Hard constraints a caller attaches to a request. Violations are never traded off."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    residency: frozenset[Jurisdiction] = Field(
        default=frozenset(), description="Allowed jurisdictions; empty means any"
    )
    requires_baa: bool = False
    max_cost_usd: float | None = Field(default=None, gt=0, le=100)


class Strategy(StrEnum):
    LEARNED = "learned"  # Thompson sampling over the family's utility
    PRIORITY = "priority"  # pool order from configuration
    COST = "cost"  # cheapest estimated cost first


class ExclusionReason(StrEnum):
    NOT_IN_POOL = "not_in_pool"
    CLASSIFICATION = "classification_ceiling"
    TASK = "task_not_served"
    NO_COMPLIANCE_PROFILE = "no_compliance_profile"
    RESIDENCY = "residency"
    BAA = "baa_required"
    COST_CEILING = "cost_ceiling"


class Exclusion(BaseModel):
    model_config = ConfigDict(frozen=True)

    provider: str
    reason: ExclusionReason
    detail: str


class Candidate(BaseModel):
    """A provider that passed every hard filter, with the scores used to order it."""

    model_config = ConfigDict(frozen=True)

    provider: str
    model: str
    estimated_cost_usd: float = Field(ge=0)
    utility: float | None = None
    sampled_quality: float | None = None
    sampled_latency_ms: float | None = None
    sampled_cost_usd: float | None = None


class RoutePlan(BaseModel):
    model_config = ConfigDict(frozen=True)

    family: str
    bucket: str
    strategy: Strategy
    candidates: tuple[Candidate, ...]
    excluded: tuple[Exclusion, ...]
    policy_version: str
    policy_digest: str

    @property
    def order(self) -> tuple[str, ...]:
        return tuple(c.provider for c in self.candidates)


class LedgerKind(StrEnum):
    ROUTE = "route"
    SHADOW = "shadow"
    EVALUATION = "evaluation"
    JUDGE = "judge"


class LedgerEntry(BaseModel):
    model_config = ConfigDict(frozen=True)

    reference: str = Field(description="Decision, evaluation run or shadow comparison id")
    kind: LedgerKind
    family: str
    provider: str
    model: str
    caller: str
    input_tokens: int = Field(ge=0)
    output_tokens: int = Field(ge=0)
    cost_usd: float = Field(ge=0)
    created_at: datetime


class LedgerGroup(StrEnum):
    PROVIDER = "provider"
    FAMILY = "family"
    CALLER = "caller"
    KIND = "kind"


class LedgerTotal(BaseModel):
    model_config = ConfigDict(frozen=True)

    key: str
    entries: int
    input_tokens: int
    output_tokens: int
    cost_usd: float


class TrafficRow(BaseModel):
    """One routed request, reduced to what reports aggregate."""

    model_config = ConfigDict(frozen=True)

    provider: str
    latency_ms: float
    cost_usd: float
    quality: float | None


class DecisionRecord(BaseModel):
    """What was routed where, without the prompt or the answer (data minimisation)."""

    model_config = ConfigDict(frozen=True)

    decision_id: str
    family: str
    bucket: str
    strategy: Strategy
    caller: str
    classification: DataClassification
    provider: str
    model: str
    candidates: tuple[str, ...]
    excluded: tuple[Exclusion, ...]
    attempts: tuple[str, ...]
    input_digest: str
    output_digest: str
    input_tokens: int
    output_tokens: int
    cost_usd: float
    latency_ms: float
    quality: float | None = None
    quality_source: str | None = None
    created_at: datetime


class ShadowOutcome(StrEnum):
    WIN = "win"  # shadow judged better than the primary
    LOSS = "loss"
    TIE = "tie"
    ERROR = "error"


class ShadowResult(BaseModel):
    model_config = ConfigDict(frozen=True)

    shadow_id: str
    decision_id: str
    family: str
    primary_provider: str
    shadow_provider: str
    outcome: ShadowOutcome
    shadow_quality: float | None
    latency_ms: float
    cost_usd: float
    detail: str
    created_at: datetime
