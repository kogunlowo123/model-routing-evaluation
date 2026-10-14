"""Evaluation domain: golden sets, graders, run results, judge calibration, promotions."""

from __future__ import annotations

import re
from datetime import datetime
from enum import StrEnum
from typing import Annotated, Any, Literal

from agentic_core.domain.classification import DataClassification
from agentic_core.domain.provenance import canonical_json, content_digest
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from model_routing_evaluation.domain.models import RoutingConstraints

_ID = re.compile(r"^[a-z0-9][a-z0-9._-]{0,62}$")


def _check_id(value: str) -> str:
    if not _ID.fullmatch(value):
        raise ValueError(f"invalid identifier {value!r}")
    return value


class ExactGrader(BaseModel):
    """Exact string match after optional whitespace stripping and case folding."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: Literal["exact"] = "exact"
    case_sensitive: bool = False
    strip: bool = True


class RegexGrader(BaseModel):
    """Every pattern must match; the score is the fraction that do."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: Literal["regex"] = "regex"
    patterns: tuple[str, ...] = Field(min_length=1, max_length=20)
    mode: Literal["search", "fullmatch"] = "search"
    ignore_case: bool = True

    @field_validator("patterns")
    @classmethod
    def _compile(cls, patterns: tuple[str, ...]) -> tuple[str, ...]:
        for pattern in patterns:
            if len(pattern) > 500:
                raise ValueError("regex pattern longer than 500 characters")
            re.compile(pattern)
        return patterns


class JsonSchemaGrader(BaseModel):
    """Output must be JSON valid against ``schema``; ``case.expected`` fields must match."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: Literal["json_schema"] = "json_schema"
    json_schema: dict[str, Any] = Field(alias="schema")


class JudgeGrader(BaseModel):
    """A model grades the output against a rubric on a 1-5 scale."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: Literal["llm_judge"] = "llm_judge"
    rubric: str = Field(min_length=20, max_length=4000)
    pass_threshold: float = Field(default=4.0, ge=1, le=5)
    judge_providers: tuple[str, ...] = Field(min_length=1)

    @property
    def rubric_digest(self) -> str:
        return content_digest(self.rubric)


GraderSpec = Annotated[
    ExactGrader | RegexGrader | JsonSchemaGrader | JudgeGrader, Field(discriminator="kind")
]


class GoldenCase(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    id: str
    input: str = Field(min_length=1, max_length=50_000)
    expected: str | dict[str, Any] | None = None
    grader: GraderSpec | None = Field(default=None, description="Overrides the set's grader")
    tags: tuple[str, ...] = ()

    @field_validator("id")
    @classmethod
    def _valid_id(cls, value: str) -> str:
        return _check_id(value)


class GoldenSet(BaseModel):
    """A versioned, reviewable set of cases with known-good answers for one task family."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str
    version: str = Field(min_length=1, max_length=32)
    task: str
    description: str = Field(min_length=10)
    classification: DataClassification = DataClassification.INTERNAL
    constraints: RoutingConstraints = RoutingConstraints()
    system_prompt: str | None = Field(default=None, max_length=8000)
    max_tokens: int = Field(default=512, ge=1, le=8192)
    grader: GraderSpec
    cases: tuple[GoldenCase, ...] = Field(min_length=1)

    @field_validator("name", "task")
    @classmethod
    def _valid_names(cls, value: str) -> str:
        return _check_id(value)

    @model_validator(mode="after")
    def _unique_cases(self) -> GoldenSet:
        ids = [c.id for c in self.cases]
        if len(ids) != len(set(ids)):
            raise ValueError(f"duplicate case ids in golden set {self.name}")
        return self

    def grader_for(
        self, case: GoldenCase
    ) -> ExactGrader | RegexGrader | JsonSchemaGrader | (JudgeGrader):
        return case.grader or self.grader

    @property
    def digest(self) -> str:
        return content_digest(canonical_json(self.model_dump(mode="json", by_alias=True)))

    @property
    def uses_judge(self) -> bool:
        return any(isinstance(self.grader_for(c), JudgeGrader) for c in self.cases)


class Grade(BaseModel):
    model_config = ConfigDict(frozen=True)

    score: float = Field(ge=0, le=1)
    passed: bool
    detail: str = ""
    judge_calibrated: bool | None = None


class CaseResult(BaseModel):
    model_config = ConfigDict(frozen=True)

    case_id: str
    provider: str
    score: float = Field(ge=0, le=1)
    passed: bool
    latency_ms: float = Field(ge=0)
    cost_usd: float = Field(ge=0)
    output: str = ""
    error: str | None = None
    detail: str = ""


class Interval(BaseModel):
    model_config = ConfigDict(frozen=True)

    low: float
    high: float


class ProviderSummary(BaseModel):
    model_config = ConfigDict(frozen=True)

    provider: str
    cases: int
    errors: int
    pass_rate: float
    pass_rate_ci95: Interval
    mean_score: float
    latency_p50_ms: float
    latency_p95_ms: float
    total_cost_usd: float
    cost_per_pass_usd: float | None


class RankedProvider(BaseModel):
    model_config = ConfigDict(frozen=True)

    provider: str
    strength: float
    strength_ci95: Interval
    wins: float
    comparisons: int


class RunSummary(BaseModel):
    model_config = ConfigDict(frozen=True)

    providers: tuple[ProviderSummary, ...]
    ranking: tuple[RankedProvider, ...] = ()
    judge_calibrated: bool | None = None
    excluded: dict[str, str] = Field(default_factory=dict)


class RunStatus(StrEnum):
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"


class EvaluationRun(BaseModel):
    model_config = ConfigDict(frozen=True)

    run_id: str
    golden_set: str
    golden_version: str
    golden_digest: str
    family: str
    providers: tuple[str, ...]
    pairwise: bool
    status: RunStatus
    requested_by: str
    estimated_cost_usd: float
    summary: RunSummary | None = None
    error: str | None = None
    created_at: datetime
    completed_at: datetime | None = None


class CalibrationItem(BaseModel):
    """A model output a human has already scored on the rubric's 1-5 scale."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    prompt: str = Field(min_length=1, max_length=20_000)
    output: str = Field(min_length=1, max_length=20_000)
    reference: str | None = Field(default=None, max_length=20_000)
    human_score: int = Field(ge=1, le=5)


class CalibrationResult(BaseModel):
    model_config = ConfigDict(frozen=True)

    calibration_id: str
    judge_provider: str
    rubric_digest: str
    items: int
    weighted_kappa: float
    spearman: float
    exact_agreement: float
    mean_bias: float
    mapping: tuple[tuple[float, float], ...] = Field(
        description="Isotonic map from raw judge score to expected human score"
    )
    calibrated: bool
    created_by: str
    created_at: datetime


class PromotionAction(StrEnum):
    ADD = "add"
    REMOVE = "remove"


class PromotionStatus(StrEnum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"


class Promotion(BaseModel):
    model_config = ConfigDict(frozen=True)

    promotion_id: str
    family: str
    provider: str
    action: PromotionAction
    run_id: str | None
    justification: str
    status: PromotionStatus
    requested_by: str
    evidence: dict[str, Any]
    policy_version: str
    policy_digest: str
    decided_by: str | None = None
    decision_reason: str | None = None
    created_at: datetime
    decided_at: datetime | None = None
