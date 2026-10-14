"""Production wiring, provider construction and published contracts."""

from __future__ import annotations

from pathlib import Path

import pytest
from agentic_core.models.factory import ProvidersConfig
from agentic_core.models.providers.bedrock import BedrockConverseProvider
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ec import SECP256R1, generate_private_key
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from model_routing_evaluation.contracts import main
from model_routing_evaluation.routing.router import PlannedRouter
from model_routing_evaluation.runtime.container import build_container, build_model_providers
from model_routing_evaluation.security.identity import FabricAuthorizer, LocalAuthorizer
from tests.conftest import ENV, ROOT, FakeClock, FakeIdp, FakeModelWorld, build, make_settings


def write_pem(path: Path, key: object) -> None:
    path.write_bytes(
        key.private_bytes(  # type: ignore[attr-defined]
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )


async def test_production_wiring_uses_audit_key_and_fabric(
    tmp_path: Path, world: FakeModelWorld, agent_idp: FakeIdp, approver_idp: FakeIdp
) -> None:
    key_file = tmp_path / "audit-2026-10.pem"
    write_pem(key_file, Ed25519PrivateKey.generate())
    settings = make_settings(
        tmp_path,
        environment="production",
        dev_ephemeral_keys=False,
        audit_key_file=key_file,
        fabric_url="https://identity.corp.test",
    )
    container = await build(settings, world, agent_idp, approver_idp, FakeClock())
    try:
        assert container.audit_key_id == "audit-2026-10"
        assert isinstance(container.access._authorizer, FabricAuthorizer)
        assert len(container.audit_public_key) == 32
    finally:
        await container.aclose()


async def test_local_mode_and_default_bedrock_construction(tmp_path: Path) -> None:
    container = await build_container(make_settings(tmp_path), env=ENV)
    try:
        assert isinstance(container.access._authorizer, LocalAuthorizer)
        nova = container.routing.router.provider("nova")
        assert isinstance(nova, BedrockConverseProvider)
    finally:
        await container.aclose()


async def test_rejects_non_ed25519_audit_key(tmp_path: Path) -> None:
    key_file = tmp_path / "audit.pem"
    write_pem(key_file, generate_private_key(SECP256R1()))
    settings = make_settings(tmp_path, dev_ephemeral_keys=False, audit_key_file=key_file)
    with pytest.raises(ValueError, match="audit key must be Ed25519"):
        await build_container(settings, env=ENV)


async def test_missing_provider_variables_fail_closed(tmp_path: Path) -> None:
    partial = {k: v for k, v in ENV.items() if k != "CLAUDE_INPUT_USD_PER_MTOK"}
    with pytest.raises(ValueError, match="CLAUDE_INPUT_USD_PER_MTOK"):
        await build_container(make_settings(tmp_path), env=partial)


def test_providers_built_from_shipped_config() -> None:
    config = ProvidersConfig.from_file(ROOT / "configs" / "providers.yaml", env=ENV)
    providers = build_model_providers(config, env=ENV)
    names = [p.profile.name for p in providers]
    assert names == ["azure-gpt", "phi", "llama", "claude", "gemini", "nova"]
    router = PlannedRouter(providers)
    with pytest.raises(ValueError, match="unknown providers"), router.planned("r", ["mystery"]):
        pass


def test_committed_contracts_are_current() -> None:
    assert main(["--check"], root=ROOT) == 0


def test_export_and_check_round_trip(tmp_path: Path) -> None:
    assert main(["--check"], root=tmp_path) == 1
    assert main([], root=tmp_path) == 0
    assert main(["--check"], root=tmp_path) == 0
    assert (tmp_path / "schemas" / "route.request.schema.json").is_file()
    assert (tmp_path / "docs" / "api" / "openapi.json").is_file()
