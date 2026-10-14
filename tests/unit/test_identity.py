"""Identity fabric client, local and fabric authorizers, human authentication, JWKS handling."""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest
from agentic_core.domain.classification import DataClassification, Decision
from agentic_core.policy import PolicyEngine, PolicySet, Resource

from model_routing_evaluation.domain.errors import (
    ForbiddenError,
    RoutingError,
    UnauthorizedError,
    UpstreamUnavailableError,
)
from model_routing_evaluation.security.identity import (
    FabricAuthorizer,
    FabricClient,
    HumanAuthenticator,
    LocalAuthorizer,
    scope_covers,
)
from model_routing_evaluation.security.jwt import JwtVerifier, RemoteJwks, StaticJwks, bearer_token
from tests.conftest import AGENT, AGENT_AUDIENCE, AGENT_ISSUER, ROOT, FakeIdp

FABRIC = "https://identity.corp.test"
DECISION = {
    "decision": "allow",
    "agent": AGENT,
    "action": "model-routing:invoke",
    "resource": "task-family:general",
    "matched_rules": ["allow-scoped-actions"],
    "policy_version": "2026.10.0",
    "policy_digest": "sha256:" + "b" * 64,
    "reason": "explicit allow",
    "delegation_chain": ["spiffe://agents.corp.test/agent/ops/orchestrator"],
}


def fabric(handler: Any) -> tuple[FabricClient, httpx.AsyncClient]:
    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return FabricClient(FABRIC + "/", client=http), http


class TestFabricClient:
    async def test_authorize_sends_bearer_and_resource(self) -> None:
        seen: dict[str, Any] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen["url"] = str(request.url)
            seen["auth"] = request.headers["authorization"]
            seen["body"] = json.loads(request.content)
            return httpx.Response(200, json=DECISION)

        client, http = fabric(handler)
        authorizer = FabricAuthorizer(client)
        decision = await authorizer.authorize(
            "agent-token",
            action="model-routing:invoke",
            resource=Resource(
                type="task-family", id="general", classification=DataClassification.CONFIDENTIAL
            ),
            environment={"purpose_of_use": "treatment"},
        )
        await http.aclose()
        assert seen["url"] == f"{FABRIC}/v1/authorize"
        assert seen["auth"] == "Bearer agent-token"
        assert seen["body"]["resource_classification"] == 2
        assert seen["body"]["environment"] == {"purpose_of_use": "treatment"}
        assert decision.decision is Decision.ALLOW
        assert decision.caller.subject == AGENT
        assert decision.caller.delegation_chain == (
            "spiffe://agents.corp.test/agent/ops/orchestrator",
        )
        assert decision.source == "identity-fabric"

    async def test_token_and_introspect(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            form = dict(httpx.QueryParams(request.content.decode()))
            if request.url.path == "/oauth2/token":
                assert form["grant_type"] == "urn:ietf:params:oauth:grant-type:jwt-bearer"
                assert form["scope"] == "model-routing:invoke"
                return httpx.Response(
                    200, json={"access_token": "t", "expires_in": 600, "scope": form["scope"]}
                )
            assert form["token"] == "t"
            return httpx.Response(
                200, json={"active": True, "sub": AGENT, "agent_status": "active"}
            )

        client, http = fabric(handler)
        issued = await client.token(
            client_id=AGENT, assertion="workload-token", scope="model-routing:invoke"
        )
        introspection = await client.introspect(issued.access_token)
        await http.aclose()
        assert issued.token_type == "Bearer"
        assert introspection.active
        assert introspection.sub == AGENT

    @pytest.mark.parametrize(
        ("status", "body", "error"),
        [
            (401, {"error": "unauthorized", "error_description": "expired"}, UnauthorizedError),
            (403, {"error": "forbidden"}, ForbiddenError),
            (400, {"error": "invalid_request"}, RoutingError),
            (503, {"error": "down"}, UpstreamUnavailableError),
        ],
    )
    async def test_error_mapping(
        self, status: int, body: dict[str, str], error: type[Exception]
    ) -> None:
        client, http = fabric(lambda _: httpx.Response(status, json=body))
        with pytest.raises(error):
            await client.introspect("t")
        await http.aclose()

    async def test_unusable_responses_fail_closed(self) -> None:
        for handler in (
            lambda _: httpx.Response(200, text="not json"),
            lambda _: httpx.Response(200, json=["list"]),
        ):
            client, http = fabric(handler)
            with pytest.raises(UpstreamUnavailableError):
                await client.introspect("t")
            await http.aclose()

        def unreachable(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("refused", request=request)

        client, http = fabric(unreachable)
        with pytest.raises(UpstreamUnavailableError, match="unreachable"):
            await client.introspect("t")
        await http.aclose()


def local_authorizer(idp: FakeIdp) -> LocalAuthorizer:
    return LocalAuthorizer(
        JwtVerifier(StaticJwks(idp.jwks()), issuer=AGENT_ISSUER, audience=AGENT_AUDIENCE),
        PolicyEngine(PolicySet.from_file(ROOT / "policies" / "agent-access.yaml")),
    )


class TestLocalAuthorizer:
    async def test_scope_allows_and_missing_scope_denies(self, agent_idp: FakeIdp) -> None:
        authorizer = local_authorizer(agent_idp)
        resource = Resource(type="task-family", id="general")
        allowed = await authorizer.authorize(
            agent_idp.mint(sub=AGENT, scope="model-routing:*"),
            action="model-routing:invoke",
            resource=resource,
            environment={},
        )
        assert allowed.decision is Decision.ALLOW
        assert allowed.source == "local-policy"
        denied = await authorizer.authorize(
            agent_idp.mint(sub=AGENT, scp=["report:read"]),
            action="model-routing:invoke",
            resource=resource,
            environment={},
        )
        assert denied.decision is Decision.DENY
        assert "default deny" in denied.reason

    async def test_delegated_credentials_are_restricted(self, agent_idp: FakeIdp) -> None:
        authorizer = local_authorizer(agent_idp)
        token = agent_idp.mint(sub=AGENT, scope="model-routing:* evaluation:run", act={"sub": "x"})
        restricted = await authorizer.authorize(
            token,
            action="model-routing:invoke",
            resource=Resource(type="task-family", classification=DataClassification.RESTRICTED),
            environment={},
        )
        assert restricted.decision is Decision.DENY
        assert restricted.caller.delegation_chain == ("x",)
        spend = await authorizer.authorize(
            token, action="evaluation:run", resource=Resource(type="golden-set"), environment={}
        )
        assert spend.matched_rules == ("deny-delegated-governance",)

    def test_scope_matching(self) -> None:
        assert scope_covers({"model-routing:*"}, "model-routing:invoke")
        assert scope_covers({"report:read"}, "report:read")
        assert not scope_covers({"model-routing:invoke"}, "model-routing:plan")
        assert not scope_covers({"model*"}, "model-routing:invoke")


class TestHumans:
    async def test_roles(self, approver_idp: FakeIdp) -> None:
        humans = HumanAuthenticator(
            JwtVerifier(
                StaticJwks(approver_idp.jwks()),
                issuer=approver_idp.issuer,
                audience=approver_idp.audience,
            )
        )
        caller = await humans.authenticate(
            approver_idp.mint(sub="dana", roles=["model-governance-approver"]),
            required_any=["model-governance-approver"],
        )
        assert caller.kind == "human"
        assert caller.subject == "dana"
        with pytest.raises(ForbiddenError, match="requires one of"):
            await humans.authenticate(
                approver_idp.mint(sub="eve", roles="not-a-list"), required_any=["x"]
            )


class TestJwt:
    def verifier(self, idp: FakeIdp) -> JwtVerifier:
        return JwtVerifier(StaticJwks(idp.jwks()), issuer=idp.issuer, audience=idp.audience)

    async def test_rejections(self, agent_idp: FakeIdp) -> None:
        verifier = self.verifier(agent_idp)
        with pytest.raises(UnauthorizedError, match="malformed"):
            await verifier.verify("not-a-jwt")
        expired = agent_idp.mint(sub=AGENT, ttl=-120)
        with pytest.raises(UnauthorizedError, match="invalid bearer token"):
            await verifier.verify(expired)
        other = FakeIdp(AGENT_ISSUER, AGENT_AUDIENCE, kid="other")
        with pytest.raises(UnauthorizedError, match="unknown signing key"):
            await verifier.verify(other.mint(sub=AGENT))
        wrong_audience = FakeIdp(AGENT_ISSUER, "someone-else", key=agent_idp.key)
        with pytest.raises(UnauthorizedError, match="invalid bearer token"):
            await verifier.verify(wrong_audience.mint(sub=AGENT))

    async def test_symmetric_algorithms_refused(self, agent_idp: FakeIdp) -> None:
        import jwt as pyjwt

        token = pyjwt.encode(
            {"sub": AGENT}, "shared-secret-value-that-is-long-enough", headers={"kid": "k1"}
        )
        with pytest.raises(UnauthorizedError, match="unsupported token algorithm"):
            await self.verifier(agent_idp).verify(token)

    def test_bearer_parsing(self) -> None:
        assert bearer_token("Bearer abc") == "abc"
        for header in (None, "", "Basic abc", "Bearer ", "Bearer " + "x" * 9000):
            with pytest.raises(UnauthorizedError):
                bearer_token(header)

    async def test_remote_jwks_caches_and_survives_outage(self, agent_idp: FakeIdp) -> None:
        state = {"calls": 0, "down": False, "now": 0.0}

        def handler(_: httpx.Request) -> httpx.Response:
            state["calls"] += 1
            if state["down"]:
                return httpx.Response(503)
            document = agent_idp.jwks()
            document["keys"].append({"kty": "oct"})  # ignored: no kid
            document["keys"].append({"kid": "broken", "kty": "EC", "crv": "P-256"})
            return httpx.Response(200, json=document)

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
            jwks = RemoteJwks(
                "https://agents.corp.test/jwks", client=http, clock=lambda: state["now"]
            )
            await jwks.key_for("k1")
            await jwks.key_for("k1")
            assert state["calls"] == 1
            with pytest.raises(UnauthorizedError):
                await jwks.key_for("missing")  # within min refresh interval: no refetch
            assert state["calls"] == 1
            state["now"], state["down"] = 400.0, True
            assert await jwks.key_for("k1") is not None  # stale keys kept during outage
            fresh = RemoteJwks("https://agents.corp.test/jwks", client=http)
            with pytest.raises(UpstreamUnavailableError):
                await fresh.key_for("k1")
