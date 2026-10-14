"""Commands and results shared by the HTTP API, MCP tools and the A2A endpoint."""

from __future__ import annotations

from typing import Any, Literal

from agentic_core.domain.classification import DataClassification
from agentic_core.models.router import Attempt
from pydantic import BaseModel, ConfigDict, Field, model_validator

from model_routing_evaluation.domain.evaluation import CalibrationItem, PromotionAction
from model_routing_evaluation.domain.models import (
    Candidate,
    Exclusion,
    RoutingConstraints,
    Strategy,
)

MAX_TOTAL_CHARS = 200_000


class ChatMessage(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    role: Literal["system", "user", "assistant"]
    content: str = Field(min_length=1, max_length=100_000)
    trust: Literal["user", "external"] = Field(
        default="user",
        description="Mark retrieved documents, web pages and tool output as external",
    )


class RouteCommand(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    task: str = Field(min_length=1, max_length=64, description="Task family, e.g. classify")
    messages: tuple[ChatMessage, ...] = Field(min_length=1, max_length=50)
    classification: DataClassification = DataClassification.INTERNAL
    max_tokens: int = Field(default=1024, ge=1, le=8192)
    temperature: float = Field(default=0.0, ge=0, le=2)
    constraints: RoutingConstraints = RoutingConstraints()
    strategy: Strategy | None = None
    response_schema: dict[str, Any] | None = Field(
        default=None, description="JSON Schema the answer must satisfy; graded immediately"
    )
    purpose_of_use: str | None = Field(default=None, min_length=2, max_length=64)

    @model_validator(mode="after")
    def _shape(self) -> RouteCommand:
        if not any(m.role != "system" for m in self.messages):
            raise ValueError("at least one user or assistant message is required")
        if sum(len(m.content) for m in self.messages) > MAX_TOTAL_CHARS:
            raise ValueError(f"messages exceed {MAX_TOTAL_CHARS} characters in total")
        return self


class Usage(BaseModel):
    model_config = ConfigDict(frozen=True)

    input_tokens: int
    output_tokens: int


class RouteResult(BaseModel):
    model_config = ConfigDict(frozen=True)

    decision_id: str
    family: str
    strategy: Strategy
    provider: str
    model: str
    text: str
    finish_reason: str
    usage: Usage
    cost_usd: float
    latency_ms: float
    candidates: tuple[Candidate, ...]
    excluded: tuple[Exclusion, ...]
    attempts: tuple[Attempt, ...]
    failed_over: bool
    schema_valid: bool | None
    redactions: int
    policy_version: str
    policy_digest: str


class FeedbackCommand(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    decision_id: str = Field(min_length=1, max_length=64)
    score: float = Field(ge=0, le=1, description="0 = unusable, 1 = fully correct")
    comment: str | None = Field(default=None, max_length=1000)


class FeedbackResult(BaseModel):
    model_config = ConfigDict(frozen=True)

    decision_id: str
    provider: str
    family: str
    score: float


class RunCommand(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    golden_set: str = Field(min_length=1, max_length=64)
    providers: tuple[str, ...] | None = Field(
        default=None, max_length=20, description="Defaults to the family's routing pool"
    )
    pairwise: bool = False
    pairwise_judges: tuple[str, ...] | None = Field(default=None, max_length=10)


class CalibrationCommand(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    judge_provider: str = Field(min_length=1, max_length=128)
    rubric: str = Field(min_length=20, max_length=4000)
    classification: DataClassification = DataClassification.INTERNAL
    items: tuple[CalibrationItem, ...] = Field(min_length=1, max_length=500)


class PromotionCommand(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    family: str = Field(min_length=1, max_length=64)
    provider: str = Field(min_length=1, max_length=128)
    action: PromotionAction = PromotionAction.ADD
    run_id: str | None = Field(default=None, max_length=64)
    justification: str = Field(min_length=20, max_length=2000)


class PromotionDecision(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    approve: bool
    reason: str = Field(min_length=5, max_length=1000)
