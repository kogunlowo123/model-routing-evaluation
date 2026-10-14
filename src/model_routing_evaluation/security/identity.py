"""Caller authentication and authorization.

Agents are authorized by the agent-identity-fabric (``/v1/authorize``) when
``MRE_FABRIC_URL`` is configured. Without a fabric, agent credentials are
verified locally against the configured issuer's JWKS and authorized by the
local access policy; there is no allow-all mode. Human approvers and auditors
authenticate with the enterprise IdP and need an explicit role.
"""

from __future__ import annotations

import fnmatch
from collections.abc import Collection
from typing import Any, Literal, Protocol

import httpx
from agentic_core.domain.classification import Decision
from agentic_core.policy import PolicyEngine, PolicyRequest, Resource, Subject
from pydantic import BaseModel, ConfigDict

from model_routing_evaluation.domain.errors import (
    ForbiddenError,
    RoutingError,
    UnauthorizedError,
    UpstreamUnavailableError,
)
from model_routing_evaluation.security.jwt import JwtVerifier

JWT_BEARER = "urn:ietf:params:oauth:grant-type:jwt-bearer"


class Caller(BaseModel):
    model_config = ConfigDict(frozen=True)

    subject: str
    kind: Literal["agent", "human"]
    roles: frozenset[str] = frozenset()
    delegation_chain: tuple[str, ...] = ()


class AccessDecision(BaseModel):
    model_config = ConfigDict(frozen=True)

    decision: Decision
    caller: Caller
    matched_rules: tuple[str, ...]
    policy_version: str
    policy_digest: str
    reason: str
    source: Literal["identity-fabric", "local-policy"]


class IssuedToken(BaseModel):
    model_config = ConfigDict(frozen=True)

    access_token: str
    # RFC 6750 token type name, not a credential.
    token_type: str = "Bearer"  # noqa: S105
    expires_in: int
    scope: str


class FabricDecision(BaseModel):
    model_config = ConfigDict(frozen=True)

    decision: Decision
    agent: str
    action: str
    resource: str
    matched_rules: tuple[str, ...]
    policy_version: str
    policy_digest: str
    reason: str
    delegation_chain: tuple[str, ...] = ()


class Introspection(BaseModel):
    model_config = ConfigDict(frozen=True)

    active: bool
    sub: str | None = None
    scope: str | None = None
    exp: int | None = None
    agent_status: str | None = None
    act: dict[str, Any] | None = None


class FabricClient:
    """Typed client for agent-identity-fabric's OAuth and authorization endpoints."""

    def __init__(self, base_url: str, *, client: httpx.AsyncClient) -> None:
        self._base = base_url.rstrip("/")
        self._client = client

    async def _post(
        self,
        path: str,
        *,
        data: dict[str, str] | None = None,
        json: dict[str, Any] | None = None,
        token: str | None = None,
    ) -> dict[str, Any]:
        headers = {"Authorization": f"Bearer {token}"} if token else {}
        try:
            response = await self._client.post(
                f"{self._base}{path}", data=data, json=json, headers=headers
            )
        except httpx.HTTPError as exc:
            raise UpstreamUnavailableError("identity fabric unreachable") from exc
        if response.status_code >= 500:
            raise UpstreamUnavailableError(f"identity fabric error {response.status_code}")
        try:
            body = response.json()
        except ValueError as exc:
            raise UpstreamUnavailableError("identity fabric returned a non-JSON body") from exc
        if not isinstance(body, dict):
            raise UpstreamUnavailableError("identity fabric returned an unexpected body")
        if response.status_code == 401:
            raise UnauthorizedError(str(body.get("error_description", "unauthorized")))
        if response.status_code == 403:
            raise ForbiddenError(str(body.get("error_description", "forbidden")))
        if response.status_code >= 400:
            raise RoutingError(
                f"identity fabric rejected the request: {body.get('error', 'invalid_request')}"
            )
        return body

    async def token(
        self, *, client_id: str, assertion: str, scope: str | None = None
    ) -> IssuedToken:
        """RFC 7523 jwt-bearer grant: exchange a workload token for an agent credential."""
        form = {"grant_type": JWT_BEARER, "client_id": client_id, "assertion": assertion}
        if scope:
            form["scope"] = scope
        return IssuedToken.model_validate(await self._post("/oauth2/token", data=form))

    async def introspect(self, token: str) -> Introspection:
        """RFC 7662 introspection, including the fabric's epoch and revocation checks."""
        return Introspection.model_validate(
            await self._post("/oauth2/introspect", data={"token": token})
        )

    async def authorize(
        self,
        token: str,
        *,
        action: str,
        resource: Resource,
        environment: dict[str, Any],
    ) -> FabricDecision:
        body = {
            "action": action,
            "resource_type": resource.type,
            "resource_id": resource.id,
            "resource_classification": int(resource.classification),
            "resource_attributes": resource.attributes,
            "environment": environment,
        }
        return FabricDecision.model_validate(
            await self._post("/v1/authorize", json=body, token=token)
        )


class Authorizer(Protocol):
    async def authorize(
        self, token: str, *, action: str, resource: Resource, environment: dict[str, Any]
    ) -> AccessDecision: ...


class FabricAuthorizer:
    def __init__(self, client: FabricClient) -> None:
        self._client = client

    async def authorize(
        self, token: str, *, action: str, resource: Resource, environment: dict[str, Any]
    ) -> AccessDecision:
        decision = await self._client.authorize(
            token, action=action, resource=resource, environment=environment
        )
        return AccessDecision(
            decision=decision.decision,
            caller=Caller(
                subject=decision.agent,
                kind="agent",
                delegation_chain=decision.delegation_chain,
            ),
            matched_rules=decision.matched_rules,
            policy_version=decision.policy_version,
            policy_digest=decision.policy_digest,
            reason=decision.reason,
            source="identity-fabric",
        )


def scope_covers(scopes: Collection[str], action: str) -> bool:
    """A scope grants an action exactly or through a trailing wildcard segment (``a:*``)."""
    return any(s == action or (s.endswith(":*") and fnmatch.fnmatchcase(action, s)) for s in scopes)


def _scopes(claims: dict[str, Any]) -> frozenset[str]:
    raw = claims.get("scope", claims.get("scp", ""))
    if isinstance(raw, str):
        return frozenset(raw.split())
    if isinstance(raw, list):
        return frozenset(str(s) for s in raw)
    return frozenset()


class LocalAuthorizer:
    """Verifies agent JWTs locally and evaluates the local access policy."""

    def __init__(self, verifier: JwtVerifier, policy: PolicyEngine) -> None:
        self._verifier = verifier
        self._policy = policy

    async def authorize(
        self, token: str, *, action: str, resource: Resource, environment: dict[str, Any]
    ) -> AccessDecision:
        claims = await self._verifier.verify(token)
        scopes = _scopes(claims)
        act = claims.get("act")
        delegated = isinstance(act, dict)
        subject = Subject(
            id=str(claims["sub"]),
            kind="agent",
            attributes={
                "scopes": sorted(scopes),
                "action_in_scope": scope_covers(scopes, action),
                "delegated": delegated,
            },
        )
        result = self._policy.evaluate(
            PolicyRequest(
                subject=subject, action=action, resource=resource, environment=environment
            )
        )
        chain: tuple[str, ...] = ()
        if delegated and isinstance(act, dict) and isinstance(act.get("sub"), str):
            chain = (str(act["sub"]),)
        return AccessDecision(
            decision=result.decision,
            caller=Caller(subject=subject.id, kind="agent", delegation_chain=chain),
            matched_rules=result.matched_rules,
            policy_version=result.policy_version,
            policy_digest=result.policy_digest,
            reason=result.reason,
            source="local-policy",
        )


class HumanAuthenticator:
    """Enterprise IdP tokens for approvers and auditors; roles come from a configured claim."""

    def __init__(self, verifier: JwtVerifier, *, roles_claim: str = "roles") -> None:
        self._verifier = verifier
        self._roles_claim = roles_claim

    async def authenticate(self, token: str, *, required_any: Collection[str]) -> Caller:
        claims = await self._verifier.verify(token)
        raw = claims.get(self._roles_claim, [])
        roles = frozenset(str(r) for r in raw) if isinstance(raw, list) else frozenset()
        if not roles & set(required_any):
            raise ForbiddenError(f"requires one of the roles {sorted(required_any)}")
        return Caller(subject=str(claims["sub"]), kind="human", roles=roles)
