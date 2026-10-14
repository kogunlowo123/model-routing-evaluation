"""Runtime settings, read from MRE_* environment variables."""

from __future__ import annotations

import socket
from pathlib import Path

from pydantic import Field, HttpUrl, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="MRE_", env_nested_delimiter="__")

    environment: str = "production"
    public_url: HttpUrl = Field(description="External base URL; used in the A2A agent card")
    database_url: str = Field(description="SQLAlchemy async URL, e.g. postgresql+asyncpg://...")

    providers_file: Path = Path("configs/providers.yaml")
    compliance_file: Path = Path("configs/compliance.yaml")
    routing_file: Path = Path("configs/routing.yaml")
    golden_sets_dir: Path = Path("configs/golden-sets")
    governance_policy_file: Path = Path("policies/model-governance.yaml")
    access_policy_file: Path = Path("policies/agent-access.yaml")

    audit_key_file: Path | None = Field(
        default=None, description="Ed25519 PEM signing key for the audit chain"
    )
    audit_chain_id: str = Field(
        default_factory=socket.gethostname,
        min_length=1,
        max_length=128,
        description="One hash chain per replica; the pod name in Kubernetes",
    )
    dev_ephemeral_keys: bool = Field(
        default=False,
        description="Generate an in-memory audit key at start-up. Refused in production.",
    )

    fabric_url: HttpUrl | None = Field(
        default=None, description="agent-identity-fabric base URL; enables fabric authorization"
    )
    agent_issuer: str | None = Field(default=None, description="Local mode: agent token issuer")
    agent_audience: str | None = Field(default=None, description="Local mode: token audience")
    agent_jwks_url: str | None = Field(default=None, description="Local mode: issuer JWKS")

    approver_issuer: str = Field(description="Enterprise OIDC issuer for approvers and auditors")
    approver_audience: str = Field(description="Audience of approver tokens")
    approver_jwks_url: str
    approver_roles_claim: str = "roles"
    approver_role: str = "model-governance-approver"
    auditor_role: str = "model-governance-auditor"

    shadow_daily_budget_usd: float = Field(
        default=0.0, ge=0, description="Shadow and shadow-judge spend cap per UTC day; 0 disables"
    )
    evaluation_concurrency: int = Field(default=4, ge=1, le=64)
    min_judge_kappa: float = Field(default=0.6, ge=0, le=1)
    min_calibration_items: int = Field(default=30, ge=5)
    mcp_allowed_hosts: list[str] = Field(
        default_factory=list,
        description="Host headers accepted by the MCP endpoint (DNS-rebinding protection)",
    )

    log_level: str = "INFO"
    otlp_endpoint: str | None = None

    @model_validator(mode="after")
    def _complete(self) -> Settings:
        if self.dev_ephemeral_keys and self.environment == "production":
            raise ValueError("dev_ephemeral_keys cannot be used in production")
        if not self.dev_ephemeral_keys and self.audit_key_file is None:
            raise ValueError("MRE_AUDIT_KEY_FILE is required unless dev_ephemeral_keys is set")
        if self.fabric_url is None:
            missing = [
                name
                for name, value in (
                    ("MRE_AGENT_ISSUER", self.agent_issuer),
                    ("MRE_AGENT_AUDIENCE", self.agent_audience),
                    ("MRE_AGENT_JWKS_URL", self.agent_jwks_url),
                )
                if not value
            ]
            if missing:
                raise ValueError(
                    "configure MRE_FABRIC_URL, or local agent token verification: "
                    f"{', '.join(missing)} missing"
                )
        return self

    @property
    def public_url_str(self) -> str:
        return str(self.public_url).rstrip("/")
