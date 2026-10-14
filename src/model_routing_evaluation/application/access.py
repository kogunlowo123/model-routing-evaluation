"""Access control shared by the HTTP API, MCP tools and the A2A endpoint."""

from __future__ import annotations

from collections.abc import Collection
from typing import Any

from agentic_core.audit import AuditLog
from agentic_core.domain.classification import DataClassification, Decision
from agentic_core.observability import correlation_id
from agentic_core.policy import Resource

from model_routing_evaluation.domain.errors import ApprovalRequiredError, ForbiddenError
from model_routing_evaluation.observability.metrics import RoutingMetrics
from model_routing_evaluation.security.identity import Authorizer, Caller, HumanAuthenticator
from model_routing_evaluation.security.jwt import bearer_token


class AccessControl:
    def __init__(
        self,
        authorizer: Authorizer,
        humans: HumanAuthenticator,
        audit: AuditLog,
        metrics: RoutingMetrics,
    ) -> None:
        self._authorizer = authorizer
        self._humans = humans
        self._audit = audit
        self._metrics = metrics

    async def agent(
        self,
        authorization: str | None,
        *,
        action: str,
        resource_type: str,
        resource_id: str = "*",
        classification: DataClassification = DataClassification.INTERNAL,
        environment: dict[str, Any] | None = None,
        channel: str = "http",
    ) -> Caller:
        """Authorize an agent call; every refusal is audited."""
        token = bearer_token(authorization)
        resource = Resource(type=resource_type, id=resource_id, classification=classification)
        decision = await self._authorizer.authorize(
            token, action=action, resource=resource, environment=environment or {}
        )
        self._metrics.authz.labels(decision.source, decision.decision.value).inc()
        if decision.decision is Decision.ALLOW:
            return decision.caller
        await self._audit.record(
            actor=decision.caller.subject,
            action="access.denied",
            resource=f"{resource_type}:{resource_id}",
            outcome=decision.decision.value,
            correlation_id=correlation_id(),
            details={
                "requested_action": action,
                "channel": channel,
                "source": decision.source,
                "matched_rules": list(decision.matched_rules),
                "policy_version": decision.policy_version,
                "policy_digest": decision.policy_digest,
                "reason": decision.reason,
            },
        )
        if decision.decision is Decision.REQUIRE_APPROVAL:
            raise ApprovalRequiredError(f"{action} requires human approval: {decision.reason}")
        raise ForbiddenError(f"{action} denied: {decision.reason}")

    async def human(self, authorization: str | None, *, roles: Collection[str]) -> Caller:
        return await self._humans.authenticate(bearer_token(authorization), required_any=roles)
