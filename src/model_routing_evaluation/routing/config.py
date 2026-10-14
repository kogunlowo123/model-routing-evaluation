"""Routing configuration: task families, utility weights, pools, shadow and promotion gates."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path

import yaml
from agentic_core.models.factory import expand_env
from pydantic import BaseModel, ConfigDict, Field, model_validator

from model_routing_evaluation.domain.errors import UnprocessableError
from model_routing_evaluation.domain.models import ComplianceProfile, Strategy


class UtilityWeights(BaseModel):
    """Relative importance of each objective. Must sum to 1."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    quality: float = Field(ge=0, le=1)
    latency: float = Field(ge=0, le=1)
    cost: float = Field(ge=0, le=1)
    compliance: float = Field(ge=0, le=1)

    @model_validator(mode="after")
    def _sum_to_one(self) -> UtilityWeights:
        total = self.quality + self.latency + self.cost + self.compliance
        if abs(total - 1.0) > 1e-6:
            raise ValueError(f"utility weights must sum to 1, got {total:.6f}")
        return self


class ShadowConfig(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    rate: float = Field(default=0.0, ge=0, le=1, description="Share of requests mirrored")
    providers: tuple[str, ...] = ()
    judge_providers: tuple[str, ...] = Field(
        default=(), description="Pairwise judges; empty disables pairwise shadow grading"
    )


class PromotionGate(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    min_pass_rate: float = Field(default=0.8, ge=0, le=1)
    min_cases: int = Field(default=10, ge=1)
    require_calibrated_judge: bool = True


class FamilyConfig(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    description: str
    weights: UtilityWeights
    latency_target_ms: float = Field(gt=0, description="Latency scored 0.5 utility")
    cost_target_usd: float = Field(gt=0, description="Per-request cost scored 0.5 utility")
    expected_output_ratio: float = Field(
        default=0.5, gt=0, le=1, description="Expected output tokens as a share of max_tokens"
    )
    pool: tuple[str, ...] = Field(min_length=1)
    shadow: ShadowConfig = ShadowConfig()
    promotion: PromotionGate = PromotionGate()


class RoutingConfig(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    default_strategy: Strategy = Strategy.LEARNED
    restricted_requires_baa: bool = True
    long_input_chars: int = Field(default=8000, ge=100)
    prior_pseudo_observations: float = Field(default=10.0, ge=0)
    offline_prior_weight: float = Field(default=0.25, ge=0, le=1)
    families: dict[str, FamilyConfig] = Field(min_length=1)

    @classmethod
    def from_file(cls, path: Path) -> RoutingConfig:
        return cls.model_validate(yaml.safe_load(path.read_text(encoding="utf-8")))

    def family(self, name: str) -> FamilyConfig:
        try:
            return self.families[name]
        except KeyError:
            raise UnprocessableError(
                f"unknown task family {name!r}",
                details={"families": sorted(self.families)},
            ) from None

    def bucket(self, input_chars: int) -> str:
        return "long" if input_chars >= self.long_input_chars else "short"

    def check_providers(self, known: set[str]) -> None:
        """Every provider a family names must exist in the provider configuration."""
        for name, family in self.families.items():
            referenced = (
                set(family.pool) | set(family.shadow.providers) | set(family.shadow.judge_providers)
            )
            unknown = referenced - known
            if unknown:
                raise ValueError(f"family {name} references unknown providers {sorted(unknown)}")


class ComplianceRegistry(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    providers: dict[str, ComplianceProfile]

    @classmethod
    def from_yaml(cls, text: str, *, env: Mapping[str, str]) -> ComplianceRegistry:
        """Contractual facts are ``${NAME}`` references; loading fails if any is unset."""
        return cls.model_validate(yaml.safe_load(expand_env(text, env)))

    @classmethod
    def from_file(cls, path: Path, *, env: Mapping[str, str]) -> ComplianceRegistry:
        return cls.from_yaml(path.read_text(encoding="utf-8"), env=env)
