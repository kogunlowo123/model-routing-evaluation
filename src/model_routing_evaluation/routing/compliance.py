"""Hard compliance filtering. Constraints here are never traded off against utility."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence

from agentic_core.domain.classification import DataClassification
from agentic_core.models.types import ModelProvider, ProviderProfile

from model_routing_evaluation.domain.models import (
    ComplianceProfile,
    Exclusion,
    ExclusionReason,
    RoutingConstraints,
)


class ComplianceFilter:
    """Removes every provider that may not receive a request.

    Checks run in a fixed order and the first failing check is reported, so
    an exclusion reason is deterministic and auditable. A provider without a
    compliance profile is excluded (fail closed): an unknown deployment has no
    established residency or contract.
    """

    def __init__(
        self, profiles: Mapping[str, ComplianceProfile], *, restricted_requires_baa: bool
    ) -> None:
        self._profiles = dict(profiles)
        self._restricted_requires_baa = restricted_requires_baa

    def profile(self, provider: str) -> ComplianceProfile | None:
        return self._profiles.get(provider)

    def requires_baa(
        self, classification: DataClassification, constraints: RoutingConstraints
    ) -> bool:
        return constraints.requires_baa or (
            self._restricted_requires_baa and classification >= DataClassification.RESTRICTED
        )

    def evaluate(
        self,
        providers: Sequence[ModelProvider],
        *,
        pool: Sequence[str],
        family: str,
        classification: DataClassification,
        constraints: RoutingConstraints,
        estimate_cost: Callable[[ProviderProfile], float],
    ) -> tuple[list[ModelProvider], list[Exclusion]]:
        allowed_pool = set(pool)
        needs_baa = self.requires_baa(classification, constraints)
        eligible: list[ModelProvider] = []
        excluded: list[Exclusion] = []
        for provider in providers:
            reason = self._reason(
                provider.profile,
                allowed_pool=allowed_pool,
                family=family,
                classification=classification,
                constraints=constraints,
                needs_baa=needs_baa,
                estimate_cost=estimate_cost,
            )
            if reason is None:
                eligible.append(provider)
            else:
                excluded.append(
                    Exclusion(provider=provider.profile.name, reason=reason[0], detail=reason[1])
                )
        return eligible, excluded

    def _reason(  # noqa: PLR0911 - one return per ordered check keeps the audit trail explicit
        self,
        profile: ProviderProfile,
        *,
        allowed_pool: set[str],
        family: str,
        classification: DataClassification,
        constraints: RoutingConstraints,
        needs_baa: bool,
        estimate_cost: Callable[[ProviderProfile], float],
    ) -> tuple[ExclusionReason, str] | None:
        if profile.name not in allowed_pool:
            return ExclusionReason.NOT_IN_POOL, f"not in the {family} routing pool"
        if classification > profile.max_classification:
            return (
                ExclusionReason.CLASSIFICATION,
                f"approved up to {profile.max_classification.name}, request is "
                f"{classification.name}",
            )
        if family not in profile.tasks and "general" not in profile.tasks:
            return ExclusionReason.TASK, f"serves {sorted(profile.tasks)}"
        facts = self._profiles.get(profile.name)
        if facts is None:
            return ExclusionReason.NO_COMPLIANCE_PROFILE, "no compliance profile configured"
        if constraints.residency and facts.jurisdiction not in constraints.residency:
            return (
                ExclusionReason.RESIDENCY,
                f"jurisdiction {facts.jurisdiction} not in {sorted(constraints.residency)}",
            )
        if needs_baa and not facts.baa:
            return ExclusionReason.BAA, "no BAA covers this deployment"
        if constraints.max_cost_usd is not None:
            estimate = estimate_cost(profile)
            if estimate > constraints.max_cost_usd:
                return (
                    ExclusionReason.COST_CEILING,
                    f"estimated {estimate:.6f} USD exceeds {constraints.max_cost_usd:.6f} USD",
                )
        return None
