"""Configuration loading, compliance filtering, golden sets and settings validation."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from agentic_core.domain.classification import DataClassification
from agentic_core.models.types import ProviderProfile

from model_routing_evaluation.config import Settings
from model_routing_evaluation.domain.errors import UnprocessableError
from model_routing_evaluation.domain.evaluation import GoldenSet
from model_routing_evaluation.domain.models import (
    ComplianceProfile,
    ExclusionReason,
    RoutingConstraints,
)
from model_routing_evaluation.evaluation.golden import load_golden_sets
from model_routing_evaluation.routing.compliance import ComplianceFilter
from model_routing_evaluation.routing.config import ComplianceRegistry, RoutingConfig
from tests.conftest import ENV, ROOT, make_settings


class FakeProvider:
    def __init__(self, profile: ProviderProfile) -> None:
        self._profile = profile

    @property
    def profile(self) -> ProviderProfile:
        return self._profile


def provider(name: str, **kwargs: Any) -> FakeProvider:
    return FakeProvider(
        ProviderProfile(
            name=name,
            model=f"{name}-model",
            max_classification=kwargs.pop("max_classification", DataClassification.RESTRICTED),
            input_cost_per_mtok=kwargs.pop("price", 1.0),
            output_cost_per_mtok=kwargs.pop("price_out", 1.0),
            tasks=frozenset(kwargs.pop("tasks", {"general"})),
        )
    )


FACTS = {
    "us-baa": ComplianceProfile(jurisdiction="us", region="eastus", baa=True),
    "us": ComplianceProfile(jurisdiction="US", region="eastus", baa=False),
    "eu": ComplianceProfile(jurisdiction="EU", region="westeurope", baa=False),
    "low-class": ComplianceProfile(jurisdiction="US", region="eastus", baa=True),
    "classify-only": ComplianceProfile(jurisdiction="US", region="eastus", baa=True),
    "pricey": ComplianceProfile(jurisdiction="US", region="eastus", baa=True),
}


class TestComplianceFilter:
    def evaluate(
        self, classification: DataClassification, constraints: RoutingConstraints
    ) -> dict[str, ExclusionReason]:
        providers = [
            provider("us-baa"),
            provider("us"),
            provider("eu"),
            provider("low-class", max_classification=DataClassification.INTERNAL),
            provider("classify-only", tasks={"classify"}),
            provider("pricey", price=1000.0),
            provider("unknown-facts"),
            provider("outside-pool"),
        ]
        flt = ComplianceFilter(FACTS, restricted_requires_baa=True)
        eligible, excluded = flt.evaluate(
            providers,  # type: ignore[arg-type]
            pool=[p.profile.name for p in providers if p.profile.name != "outside-pool"],
            family="summarize",
            classification=classification,
            constraints=constraints,
            estimate_cost=lambda p: p.estimate_cost(1000, 1000),
        )
        result = {e.provider: e.reason for e in excluded}
        result |= {p.profile.name: "eligible" for p in eligible}  # type: ignore[misc]
        return result

    def test_every_exclusion_reason(self) -> None:
        result = self.evaluate(
            DataClassification.CONFIDENTIAL,
            RoutingConstraints(residency=frozenset({"US"}), max_cost_usd=0.5),
        )
        assert result == {
            "us-baa": "eligible",
            "us": "eligible",
            "eu": ExclusionReason.RESIDENCY,
            "low-class": ExclusionReason.CLASSIFICATION,
            "classify-only": ExclusionReason.TASK,
            "pricey": ExclusionReason.COST_CEILING,
            "unknown-facts": ExclusionReason.NO_COMPLIANCE_PROFILE,
            "outside-pool": ExclusionReason.NOT_IN_POOL,
        }

    def test_restricted_data_requires_baa(self) -> None:
        result = self.evaluate(DataClassification.RESTRICTED, RoutingConstraints())
        assert result["us-baa"] == "eligible"
        assert result["us"] == ExclusionReason.BAA
        assert result["eu"] == ExclusionReason.BAA

    def test_explicit_baa_constraint(self) -> None:
        result = self.evaluate(DataClassification.INTERNAL, RoutingConstraints(requires_baa=True))
        assert result["us"] == ExclusionReason.BAA
        assert result["pricey"] == "eligible"

    def test_posture_score(self) -> None:
        both = ComplianceProfile(jurisdiction="US", region="r", baa=True, zero_data_retention=True)
        assert both.posture_score == 1.0
        assert FACTS["us"].posture_score == 0.0

    @pytest.mark.parametrize("code", ["U", "toolongcode", "U$"])
    def test_invalid_jurisdiction(self, code: str) -> None:
        with pytest.raises(ValueError, match="jurisdiction"):
            ComplianceProfile(jurisdiction=code, region="r", baa=False)


class TestRoutingConfig:
    def test_shipped_config_is_consistent(self) -> None:
        config = RoutingConfig.from_file(ROOT / "configs" / "routing.yaml")
        config.check_providers({"azure-gpt", "phi", "llama", "claude", "gemini", "nova"})
        assert config.bucket(10) == "short"
        assert config.bucket(config.long_input_chars) == "long"
        with pytest.raises(UnprocessableError, match="unknown task family"):
            config.family("poetry")

    def test_unknown_provider_in_family(self) -> None:
        config = RoutingConfig.from_file(ROOT / "configs" / "routing.yaml")
        with pytest.raises(ValueError, match="unknown providers"):
            config.check_providers({"azure-gpt"})

    def test_compliance_registry_fails_closed_on_missing_env(self) -> None:
        text = (ROOT / "configs" / "compliance.yaml").read_text(encoding="utf-8")
        registry = ComplianceRegistry.from_yaml(text, env=ENV)
        assert registry.providers["gemini"].jurisdiction == "EU"
        assert registry.providers["azure-gpt"].baa is True
        partial = {k: v for k, v in ENV.items() if k != "GEMINI_BAA"}
        with pytest.raises(ValueError, match="GEMINI_BAA"):
            ComplianceRegistry.from_yaml(text, env=partial)


class TestGoldenSets:
    def test_shipped_sets_load(self) -> None:
        sets = load_golden_sets(ROOT / "configs" / "golden-sets")
        assert sets["support-ticket-triage"].task == "classify"
        assert len(sets["support-ticket-triage"].cases) == 12
        assert sets["incident-summary"].uses_judge
        assert not sets["invoice-field-extraction"].uses_judge
        assert sets["invoice-field-extraction"].digest.startswith("sha256:")

    def base(self, **overrides: Any) -> dict[str, Any]:
        data: dict[str, Any] = {
            "name": "set-a",
            "version": "1",
            "task": "classify",
            "description": "a valid description",
            "grader": {"kind": "exact"},
            "cases": [{"id": "c1", "input": "x", "expected": "y"}],
        }
        data.update(overrides)
        return data

    def test_validation(self) -> None:
        GoldenSet.model_validate(self.base())
        with pytest.raises(ValueError, match="duplicate case ids"):
            GoldenSet.model_validate(
                self.base(cases=[{"id": "c1", "input": "x"}, {"id": "c1", "input": "y"}])
            )
        with pytest.raises(ValueError, match="invalid identifier"):
            GoldenSet.model_validate(self.base(name="Bad Name"))
        with pytest.raises(ValueError, match="invalid identifier"):
            GoldenSet.model_validate(self.base(cases=[{"id": "C 1", "input": "x"}]))

    def test_duplicate_names_across_files(self, tmp_path: Path) -> None:
        import yaml

        for file in ("a.yaml", "b.yaml"):
            (tmp_path / file).write_text(yaml.safe_dump(self.base()), encoding="utf-8")
        with pytest.raises(ValueError, match="duplicate golden set name"):
            load_golden_sets(tmp_path)


class TestSettings:
    def test_production_refuses_ephemeral_keys(self, tmp_path: Path) -> None:
        with pytest.raises(ValueError, match="cannot be used in production"):
            make_settings(tmp_path, environment="production")

    def test_audit_key_required(self, tmp_path: Path) -> None:
        with pytest.raises(ValueError, match="MRE_AUDIT_KEY_FILE"):
            make_settings(tmp_path, dev_ephemeral_keys=False)

    def test_local_mode_needs_issuer_settings(self, tmp_path: Path) -> None:
        with pytest.raises(ValueError, match="MRE_AGENT_JWKS_URL"):
            make_settings(tmp_path, agent_jwks_url=None)

    def test_fabric_mode_needs_no_local_issuer(self, tmp_path: Path) -> None:
        settings: Settings = make_settings(
            tmp_path,
            fabric_url="https://identity.corp.test",
            agent_issuer=None,
            agent_audience=None,
            agent_jwks_url=None,
        )
        assert str(settings.fabric_url) == "https://identity.corp.test/"
        assert settings.public_url_str == "https://routing.corp.test"
