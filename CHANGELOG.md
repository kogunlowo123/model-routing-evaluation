# Changelog

Format: [Keep a Changelog](https://keepachangelog.com/en/1.1.0/); versions follow [SemVer](https://semver.org/).

## [0.1.0] - 2026-10-14

### Added

- Routing service on the agentic-core `ModelRouter` with a per-request planned
  provider order; strategies `learned`, `priority` and `cost`.
- Contextual Thompson-sampling bandit over a quality, latency, cost and
  compliance-posture utility, with additive sufficient statistics in PostgreSQL
  and empirical-Bayes backoff from input-size buckets to the task family.
- Hard compliance filters: routing pool, classification ceiling, task support,
  compliance profile, residency jurisdiction, BAA (always for RESTRICTED data)
  and per-request cost ceiling. Shadows and judges pass the same filter.
- Content controls on every input and output through agentic-core `ContentGuard`.
- Offline evaluation harness: golden sets, exact / regex / JSON Schema /
  LLM-judge graders, Wilson intervals, pairwise preference with position swap,
  Bradley-Terry ranking with case-bootstrap intervals, offline prior into the bandit.
- Judge calibration against human scores (quadratic-weighted kappa, Spearman,
  isotonic mapping).
- Online shadow evaluation with a daily spend budget.
- Cost ledger and per-family reports (JSON and Markdown).
- Governed routing-pool promotions with evidence gates and two-person approval.
- Identity: agent-identity-fabric client (`/oauth2/token`, `/oauth2/introspect`,
  `/v1/authorize`) or local JWKS verification with a local access policy;
  enterprise OIDC roles for approvers and auditors.
- MCP tools at `/mcp`; A2A Agent Card and JSON-RPC `message/send`.
- Per-replica, signed audit chains in an append-only PostgreSQL table.
- Helm chart, Kubernetes namespace and secret sync, Terraform for Azure
  (primary), AWS and GCP.
- CI: lint, types, SAST, contract drift, tests (unit, security, chaos,
  performance, e2e, PostgreSQL integration), Helm and kubeconform, Terraform
  validate, Trivy, Syft, Grype, Semgrep, container smoke test.
