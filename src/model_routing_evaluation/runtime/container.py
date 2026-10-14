"""Composition root: builds every component once per process from Settings."""

from __future__ import annotations

import asyncio
import os
import random
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass

import httpx
from agentic_core.audit import AuditLog
from agentic_core.models.factory import ProviderKind, ProvidersConfig, build_provider
from agentic_core.models.providers.bedrock import BedrockConverseProvider, ConverseClient
from agentic_core.models.resilience import RetryPolicy
from agentic_core.models.router import Sleep
from agentic_core.models.types import ModelProvider
from agentic_core.observability import ModelTelemetryObserver
from agentic_core.policy import PolicyEngine, PolicySet
from agentic_core.security import ContentGuard
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from sqlalchemy.ext.asyncio import AsyncEngine

from model_routing_evaluation.application.access import AccessControl
from model_routing_evaluation.application.evaluation_service import EvaluationService
from model_routing_evaluation.application.reports import ReportBuilder
from model_routing_evaluation.application.routing_service import RoutingService
from model_routing_evaluation.config import Settings
from model_routing_evaluation.domain.ports import Clock
from model_routing_evaluation.evaluation.golden import load_golden_sets
from model_routing_evaluation.evaluation.harness import OfflineEvaluator
from model_routing_evaluation.evaluation.judge import LlmJudge
from model_routing_evaluation.infrastructure.database import (
    SqlAuditSink,
    SqlBanditStore,
    SqlDecisionRepository,
    SqlEvaluationRepository,
    SqlLedger,
    SqlPromotionRepository,
    SqlShadowRepository,
    SystemClock,
    create_schema,
    make_engine,
)
from model_routing_evaluation.observability.metrics import RoutingMetrics
from model_routing_evaluation.routing.bandit import ThompsonSampler
from model_routing_evaluation.routing.compliance import ComplianceFilter
from model_routing_evaluation.routing.config import ComplianceRegistry, RoutingConfig
from model_routing_evaluation.routing.router import PlannedRouter
from model_routing_evaluation.security.identity import (
    Authorizer,
    FabricAuthorizer,
    FabricClient,
    HumanAuthenticator,
    LocalAuthorizer,
)
from model_routing_evaluation.security.jwt import JwtVerifier, KeyResolver, RemoteJwks

ConverseFactory = Callable[[str], ConverseClient]


@dataclass
class Container:
    settings: Settings
    routing: RoutingService
    evaluation: EvaluationService
    reports: ReportBuilder
    access: AccessControl
    audit: AuditLog
    audit_public_key: bytes
    audit_key_id: str
    ledger: SqlLedger
    metrics: RoutingMetrics
    engine: AsyncEngine
    http: httpx.AsyncClient
    clock: Clock

    async def aclose(self) -> None:
        await self.routing.drain()
        await self.evaluation.drain()
        await self.routing.router.aclose()
        await self.http.aclose()
        await self.engine.dispose()


def build_model_providers(
    config: ProvidersConfig,
    *,
    env: Mapping[str, str],
    transport: httpx.AsyncBaseTransport | None = None,
    converse_factory: ConverseFactory | None = None,
) -> Sequence[ModelProvider]:
    """agentic-core adapters; Bedrock clients can be injected (tests, custom sessions)."""
    providers: list[ModelProvider] = []
    for spec in config.providers:
        region = spec.region or spec.profile.region
        if spec.kind is ProviderKind.BEDROCK and converse_factory is not None and region:
            providers.append(BedrockConverseProvider(spec.profile, client=converse_factory(region)))
        else:
            providers.append(build_provider(spec, env=env, transport=transport))
    return providers


def _audit_signer(settings: Settings) -> tuple[Ed25519PrivateKey, str]:
    if settings.audit_key_file is None:
        return Ed25519PrivateKey.generate(), "audit-ephemeral"
    key = serialization.load_pem_private_key(settings.audit_key_file.read_bytes(), password=None)
    if not isinstance(key, Ed25519PrivateKey):
        raise ValueError("audit key must be Ed25519")
    return key, settings.audit_key_file.stem


async def build_container(
    settings: Settings,
    *,
    env: Mapping[str, str] | None = None,
    transport: httpx.AsyncBaseTransport | None = None,
    converse_factory: ConverseFactory | None = None,
    http: httpx.AsyncClient | None = None,
    agent_resolver: KeyResolver | None = None,
    approver_resolver: KeyResolver | None = None,
    clock: Clock | None = None,
    rng: random.Random | None = None,
    retry: RetryPolicy | None = None,
    sleep: Sleep | None = None,
) -> Container:
    env = os.environ if env is None else env
    clock = clock or SystemClock()
    rng = rng or random.SystemRandom()
    http = http or httpx.AsyncClient(timeout=httpx.Timeout(5.0))

    providers = build_model_providers(
        ProvidersConfig.from_file(settings.providers_file, env=env),
        env=env,
        transport=transport,
        converse_factory=converse_factory,
    )
    known = {p.profile.name for p in providers}
    routing_config = RoutingConfig.from_file(settings.routing_file)
    routing_config.check_providers(known)
    registry = ComplianceRegistry.from_file(settings.compliance_file, env=env)
    golden_sets = load_golden_sets(settings.golden_sets_dir)
    for golden in golden_sets.values():
        routing_config.family(golden.task)

    engine = make_engine(settings.database_url)
    await create_schema(engine)
    signer, key_id = _audit_signer(settings)
    audit = AuditLog(SqlAuditSink(engine, settings.audit_chain_id), signer, key_id=key_id)

    metrics = RoutingMetrics()
    router = PlannedRouter(
        providers,
        retry=retry,
        observers=[ModelTelemetryObserver()],
        sleep=sleep or asyncio.sleep,
    )
    guard = ContentGuard()
    judge = LlmJudge(router)
    sampler = ThompsonSampler(
        rng, prior_pseudo_observations=routing_config.prior_pseudo_observations
    )
    bandit = SqlBanditStore(engine)
    decisions = SqlDecisionRepository(engine)
    ledger = SqlLedger(engine)
    shadows = SqlShadowRepository(engine)
    runs = SqlEvaluationRepository(engine)
    promotions = SqlPromotionRepository(engine)
    governance = PolicyEngine(PolicySet.from_file(settings.governance_policy_file))
    compliance = ComplianceFilter(
        registry.providers, restricted_requires_baa=routing_config.restricted_requires_baa
    )

    routing = RoutingService(
        router=router,
        config=routing_config,
        compliance=compliance,
        sampler=sampler,
        bandit=bandit,
        decisions=decisions,
        ledger=ledger,
        shadows=shadows,
        promotions=promotions,
        governance=governance,
        guard=guard,
        judge=judge,
        audit=audit,
        metrics=metrics,
        clock=clock,
        rng=rng,
        shadow_daily_budget_usd=settings.shadow_daily_budget_usd,
    )
    evaluation = EvaluationService(
        routing=routing,
        evaluator=OfflineEvaluator(
            router, judge, guard, rng=rng, concurrency=settings.evaluation_concurrency
        ),
        judge=judge,
        golden_sets=golden_sets,
        runs=runs,
        promotions=promotions,
        ledger=ledger,
        bandit=bandit,
        governance=governance,
        audit=audit,
        metrics=metrics,
        clock=clock,
        min_judge_kappa=settings.min_judge_kappa,
        min_calibration_items=settings.min_calibration_items,
    )
    reports = ReportBuilder(
        routing=routing,
        sampler=sampler,
        bandit=bandit,
        decisions=decisions,
        ledger=ledger,
        shadows=shadows,
        runs=runs,
        promotions=promotions,
        clock=clock,
    )

    authorizer: Authorizer
    if settings.fabric_url is not None:
        authorizer = FabricAuthorizer(FabricClient(str(settings.fabric_url), client=http))
    else:
        if not (settings.agent_issuer and settings.agent_audience and settings.agent_jwks_url):
            raise ValueError("local agent verification is not configured")  # pragma: no cover
        authorizer = LocalAuthorizer(
            JwtVerifier(
                agent_resolver or RemoteJwks(settings.agent_jwks_url, client=http),
                issuer=settings.agent_issuer,
                audience=settings.agent_audience,
            ),
            PolicyEngine(PolicySet.from_file(settings.access_policy_file)),
        )
    humans = HumanAuthenticator(
        JwtVerifier(
            approver_resolver or RemoteJwks(settings.approver_jwks_url, client=http),
            issuer=settings.approver_issuer,
            audience=settings.approver_audience,
        ),
        roles_claim=settings.approver_roles_claim,
    )
    await evaluation.recover()
    return Container(
        settings=settings,
        routing=routing,
        evaluation=evaluation,
        reports=reports,
        access=AccessControl(authorizer, humans, audit, metrics),
        audit=audit,
        audit_public_key=signer.public_key().public_bytes(
            serialization.Encoding.Raw, serialization.PublicFormat.Raw
        ),
        audit_key_id=key_id,
        ledger=ledger,
        metrics=metrics,
        engine=engine,
        http=http,
        clock=clock,
    )
