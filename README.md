# Model Routing and Evaluation Platform

Routes every model call to the best eligible foundation model (GPT on Azure
OpenAI, Claude, Gemini, Nova on Bedrock, Llama and Phi on Azure AI Foundry)
for its task family, and measures whether that choice was right. Data
residency, BAA coverage, data classification and cost ceilings are hard
filters applied before any optimisation. Among the providers that survive
them, a contextual Thompson-sampling bandit trades off quality, latency, cost
and compliance posture, and learns from schema checks, caller feedback,
shadow comparisons and offline evaluation. Changes to a family's routing pool
require evaluation evidence and a human approver who is not the requester.

```
Task ─▶ Identity ─▶ Governance ─▶ Compliance filter ─▶ Learned router
     ─▶ GPT / Claude / Gemini / Nova / Llama / Phi ─▶ Guardrails ─▶ Output
     ─▶ Ledger, audit, metrics ─▶ Shadow and offline evaluation ─▶ Router
```

## Capabilities

| Capability | Implementation |
|---|---|
| Multi-provider routing | agentic-core `ModelRouter` (retries, circuit breakers, failover) with a per-request planned order; unplanned calls are refused |
| Learned routing | Contextual Thompson sampling per (task family, input-size bucket, provider); Beta quality, Normal-Inverse-Gamma log latency, Gamma-scaled cost; empirical-Bayes backoff to the family arm |
| Hard compliance filters | Pool membership, classification ceiling, task support, compliance profile present, residency jurisdiction, BAA (always for RESTRICTED), per-request cost ceiling; the same filter gates shadows and judges |
| Strategies | `learned` (default), `priority` (family pool order), `cost` (price-sheet estimate) |
| Offline evaluation | Versioned golden sets; exact, regex, JSON Schema and LLM-judge graders; Wilson intervals; pairwise preference with Bradley-Terry ranking and case-bootstrap intervals |
| Judge calibration | Quadratic-weighted kappa, Spearman correlation and an isotonic map against human scores; promotion gates can require a calibrated judge |
| Shadow evaluation | Sampled mirroring to shadow providers, position-debiased pairwise judging or schema grading, capped by a daily budget |
| Governance | Policy-as-code (`policies/model-governance.yaml`); pool changes need evidence plus a two-person approval |
| Cost ledger | Every routed, shadow, evaluation and judge call priced from configured contract rates; totals by provider, family, caller or kind |
| Reports | Per task family: traffic, spend, latency, feedback, posteriors, shadow win rate, offline pass rate and strength; JSON or Markdown |
| Content controls | agentic-core `ContentGuard` on every input (injection, secrets, PII redaction) and output (credential leakage, PII redaction) |
| Identity | Agent calls authorized by agent-identity-fabric `/v1/authorize`, or verified locally against an issuer JWKS with a local access policy; humans via enterprise OIDC roles |
| MCP and A2A | MCP tools at `/mcp` (official `mcp` SDK, streamable HTTP); A2A Agent Card and JSON-RPC `message/send` |
| Audit | Hash-chained, Ed25519-signed events per replica in an append-only PostgreSQL table; verification endpoint |
| Observability | Prometheus metrics, OpenTelemetry GenAI traces, JSON logs with correlation IDs, SLO alerts, Grafana dashboard |

Shared controls (model adapters and router, policy engine, audit chain,
content guard, telemetry) come from
[agentic-core](https://github.com/kogunlowo123/agentic-core).

## Quick start (local)

Requires Docker. No API keys or cloud credentials are needed.

```bash
docker compose up --build
curl -sf localhost:8080/readyz
curl -s localhost:8080/.well-known/agent-card.json | jq '.skills[].id'
```

The compose stack runs PostgreSQL, an OIDC provider that issues agent
credentials and approver tokens, and an OpenTelemetry collector. Model
endpoints point at a host that does not resolve, so model calls fail over and
return 503; planning, compliance filtering, reports, the ledger, MCP and A2A
discovery work without any vendor account.

## Development

```bash
uv sync
make check        # ruff, black, mypy --strict, bandit, contracts, tests (coverage >= 90%)
make integration  # PostgreSQL integration tests (needs MRE_TEST_DATABASE_URL)
make helm terraform
```

Test suites: `tests/unit`, `tests/security` (attacks mapped to the threat
model), `tests/chaos` (provider, database, fabric and audit failures),
`tests/performance` (latency budgets; `locustfile.py` for load), `tests/e2e`
(full HTTP lifecycle, MCP, A2A and a property-based OpenAPI contract test),
`tests/integration` (PostgreSQL). Every vendor API is simulated at the wire
level with `httpx.MockTransport` and a fake Bedrock Converse client.

## API

| Endpoint | Caller | Action / role | Purpose |
|---|---|---|---|
| `POST /v1/route` | Agent | `model-routing:invoke` | Route a completion and return the answer, plan and cost |
| `POST /v1/route/plan` | Agent | `model-routing:plan` | Dry run: eligible providers, exclusion reasons, order |
| `POST /v1/feedback` | Agent that made the request | `model-routing:feedback` | Rate a decision once (0 to 1) |
| `GET /v1/providers` | Agent | `provider:read` | Providers, compliance facts, circuit state |
| `GET /v1/golden-sets` | Agent | `evaluation:read` | Golden sets and digests |
| `POST /v1/evaluations` | Agent | `evaluation:run` | Start an offline run (202); poll `GET /v1/evaluations/{run_id}` |
| `GET /v1/evaluations/{run_id}` | Agent | `evaluation:read` | Run status, summary, per-case results |
| `POST /v1/judges/calibrations` | Agent | `evaluation:calibrate` | Calibrate a judge against human scores |
| `GET /v1/reports/{family}` | Agent | `report:read` | Family report (`?format=markdown`, `?days=`) |
| `GET /v1/ledger` | Agent | `ledger:read` | Spend totals (`?group_by=provider\|family\|caller\|kind&days=`) |
| `POST /v1/promotions` | Agent | `promotion:request` | Propose adding or removing a provider in a pool |
| `GET /v1/promotions` | Agent | `report:read` | List promotions |
| `POST /v1/promotions/{id}/decision` | Human | `model-governance-approver` | Approve or reject (not the requester) |
| `GET /v1/audit/verify` | Human | `model-governance-auditor` or approver | Verify every replica's audit chain |
| `GET /.well-known/agent-card.json` | Anyone | | A2A Agent Card |
| `POST /a2a` | Agent | per skill | A2A JSON-RPC `message/send` |
| `POST /mcp` | Agent | per tool | MCP tools `route_completion`, `plan_route`, `list_providers`, `family_report` |
| `GET /healthz`, `/readyz`, `/metrics` | Platform | | Liveness, readiness (database), Prometheus |

Full contract: [docs/api/openapi.json](docs/api/openapi.json); JSON Schemas in [schemas/](schemas/).
Errors use OAuth-style bodies: `{"error": "...", "error_description": "...", "details": {...}}`.

## Deployment

| Layer | Location |
|---|---|
| Container image | `Dockerfile` (non-root uid 10001, read-only root filesystem compatible) |
| Helm chart | `deployments/helm/model-routing-evaluation` (Deployment, PDB, HPA, NetworkPolicy, optional ServiceMonitor and Ingress) |
| Namespace and secret sync | `deployments/kubernetes` (restricted Pod Security, default deny, External Secrets) |
| Cloud infrastructure | `deployments/terraform/azure` (primary: PostgreSQL Flexible Server, Key Vault, Azure OpenAI, AKS workload identity), `aws` (Bedrock access for Nova, RDS, KMS, Secrets Manager), `gcp` (Cloud SQL, KMS, Secret Manager) |

See the [startup runbook](docs/runbooks/startup.md).

## Documentation

- [Architecture](docs/architecture/overview.md) and [agent specification](docs/architecture/agent-specification.md)
- [ADRs](docs/architecture/adr/)
- [Evaluation methodology](docs/evaluation/methodology.md)
- [Threat model and risk register](docs/security/threat-model.md)
- [Compliance control mapping](docs/compliance/control-mapping.md)
- Runbooks: [startup](docs/runbooks/startup.md), [monitoring](docs/runbooks/monitoring.md), [incident response](docs/runbooks/incident-response.md), [provider outage](docs/runbooks/provider-outage.md), [cost spike](docs/runbooks/cost-spike.md), [promotion approval](docs/runbooks/promotion-approval.md), [rollback](docs/runbooks/rollback.md), [disaster recovery](docs/runbooks/disaster-recovery.md)
- [Configuration reference](docs/operations/configuration.md)

## License

Apache-2.0
