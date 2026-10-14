# Agent specification

The routing service acts on behalf of other agents: it decides which external
model receives their data and spends money on their behalf. This document
states its mandate in the same terms agent-identity-fabric requires of every
registered agent.

## Role and mission

- **Purpose:** be the single, governed decision point for foundation model
  calls across vendors.
- **Business objective:** every model call goes to a provider the data may
  legally and contractually reach, at the best measured trade-off between
  quality, latency, cost and compliance posture for that task family, with
  spend attributable to a caller and a task.
- **Protected resources:** request and response content in flight, provider
  API keys, compliance profiles, routing pools, bandit statistics, evaluation
  evidence, the cost ledger and audit chains.
- **Operating boundaries:** never sends data to a provider that fails a hard
  filter; never persists routed prompts or answers; never changes a routing
  pool without evidence and a human decision; never acts on instructions found
  inside model outputs (they are judged as data).
- **Delegated responsibility:** caller authorization is delegated to
  agent-identity-fabric; governance rules to `policies/model-governance.yaml`
  owned by AI governance; contractual facts to `configs/compliance.yaml`
  values owned by legal and procurement.

## Delegated scope

| Capability | Allowed | Disallowed | Risk | Escalation |
|---|---|---|---|---|
| Route completions | To providers passing every hard filter, in the planned order | Any provider outside the family pool, above its classification ceiling, outside the requested residency, without a BAA where required, above the cost ceiling | MODERATE | 422 with exclusion reasons; RESTRICTED data without purpose of use is denied by policy |
| Fail over | Within the planned order using agentic-core retry and circuit breaker | Any implicit default route (unplanned calls are refused) | MODERATE | 503 after all planned providers fail; `RouteFailureRatioHigh` alert |
| Learn | Update arm statistics from schema grades, caller feedback (once per decision, by the calling agent), shadow and offline results | Feedback from another agent; repeated feedback | LOW | 403 / 409 |
| Shadow evaluation | Mirror a sampled share of traffic to compliance-eligible shadow providers within the daily budget | Shadow to a provider the primary request could not use; exceeding the budget | MODERATE | Budget exhaustion is counted in `mre_shadow_comparisons_total{outcome="budget_exhausted"}` |
| Offline evaluation | Run golden sets against cleared providers and judges | Runs estimated above the governance budget; judges not cleared for the data | MODERATE | 403 from policy |
| Change routing pools | Record a request with evidence | Applying a change without approval; approval by the requester or by an agent | HIGH | Pending until a `model-governance-approver` decides |
| Withhold output | Block outputs carrying credentials; redact PII | Returning blocked output | HIGH | 502 `output_blocked`, `GuardBlockSpike` alert |

## Objectives

| Type | Definition | Target |
|---|---|---|
| SLO availability | Non-5xx share of `/v1/route` responses excluding 503 caused by every vendor failing | 99.9% over 30 days |
| SLO latency (overhead) | p99 of request duration minus model latency on `/v1/route` | < 150 ms |
| SLO latency | p99 `/v1/route/plan` | < 250 ms |
| SLI | `mre:request_errors:ratio_5m`, `mre:route_failures:ratio_15m`, latency histograms | Prometheus |
| KPI | Share of model calls in the platform that pass through this service | 100% |
| KPI | Hard-filter violations (requests served by an excluded provider) | 0 |
| KPI | Share of ledger cost attributed to a caller and family | 100% |
| KPI | Families whose pool changed without approved evidence | 0 |
| Completion | A routing request is complete when its decision row, ledger entry and audit event are written | Enforced: audit failure fails the request |

## Permissions

| Mechanism | Use |
|---|---|
| Agent identity | SPIFFE IDs issued by agent-identity-fabric; the authorized `agent` and `delegation_chain` are recorded in every audit event |
| OAuth 2.0 | Agents present fabric credentials (RFC 7523 jwt-bearer issuance); the service calls `/v1/authorize` per request; `/oauth2/introspect` and `/oauth2/token` are available through the typed client |
| ABAC (local mode) | `policies/agent-access.yaml`: scopes must cover the action (`a:*` wildcard); delegated credentials cannot reach RESTRICTED data, run evaluations, calibrate judges or request promotions |
| Governance ABAC | `policies/model-governance.yaml`: routing allowed per task family, RESTRICTED needs `purpose_of_use`, evaluation budget, pool changes require evidence and approval |
| RBAC (humans) | Enterprise OIDC roles `model-governance-approver` (decide promotions, verify audit) and `model-governance-auditor` (verify audit) |
| OIDC | Approver and auditor tokens verified against the enterprise IdP JWKS (issuer, audience, expiry, asymmetric algorithms only) |
| Workload identity | AKS workload identity for Key Vault and Azure OpenAI; projected service-account token federated to an AWS IAM role for Bedrock |

Agent actions: `model-routing:invoke`, `model-routing:plan`,
`model-routing:feedback`, `evaluation:run`, `evaluation:read`,
`evaluation:calibrate`, `report:read`, `ledger:read`, `provider:read`,
`promotion:request`.

## Inputs and outputs

| Contract | Schema |
|---|---|
| Route request / response | `schemas/route.request.schema.json`, `schemas/route.response.schema.json` |
| Route plan | `schemas/route-plan.response.schema.json` |
| Feedback | `schemas/feedback.request.schema.json` |
| Evaluation run | `schemas/evaluation-run.request.schema.json`, `schemas/evaluation-run.response.schema.json` |
| Judge calibration | `schemas/judge-calibration.request.schema.json`, `schemas/judge-calibration.response.schema.json` |
| Promotion | `schemas/promotion.request.schema.json`, `schemas/promotion-decision.request.schema.json`, `schemas/promotion.response.schema.json` |
| Family report | `schemas/family-report.response.schema.json` |
| Stored records | `schemas/decision-record.schema.json`, `schemas/ledger-entry.schema.json` |
| Configuration | `schemas/golden-set.schema.json`, `schemas/routing-config.schema.json`, `schemas/compliance-profile.schema.json` |

The committed schemas and `docs/api/openapi.json` are regenerated by
`python -m model_routing_evaluation.contracts`; CI fails if they drift.

## Validation

- Request bodies reject unknown fields. Messages: 1 to 50, each up to 100,000
  characters, at most 200,000 in total, at least one non-system message.
- Message trust is `user` or `external`; callers cannot claim system or
  verified trust. External content is spotlighted between random boundaries.
- `response_schema` from callers is bounded (16 KiB, depth 12), must be a
  valid Draft 2020-12 schema and may not use `pattern`, `patternProperties`,
  `format` or any reference keyword (ReDoS and remote-resolution defence).
- Residency codes are 2 to 8 alphanumeric characters; cost ceilings are
  positive and at most 100 USD.
- Configuration (providers, compliance, routing, golden sets, policies) is
  validated at start-up; any unset `${NAME}` reference or unknown provider in
  a family stops the service.

## Provenance

- Each decision stores SHA-256 digests of the sanitized input and of the
  returned output, not the text. A caller holding the content can prove which
  answer was served by recomputing the digest.
- Evaluation runs record the golden set's name, version and canonical digest;
  promotions copy that digest into their evidence.
- Every governance decision records the policy name, version and digest.

## Audit metadata

Every event carries actor, action, resource, outcome, correlation ID, a
sequence number, the previous event's hash, its own hash and an Ed25519
signature. Actions emitted:

| Action | Resource | Details |
|---|---|---|
| `route.completed` | `decision:<id>` | family, bucket, strategy, candidates, exclusions, provider, model, cost, attempts, output digest, delegation chain, policy digest |
| `route.rejected` | `decision:<id>` | no eligible provider, exclusions |
| `route.blocked` | `decision:<id>` | input or output blocked, finding kinds |
| `route.failed` | `decision:<id>` | every provider failed, attempts |
| `route.feedback` | `decision:<id>` | provider, family, score |
| `shadow.compared` | `decision:<id>` | primary, shadow, outcome, cost |
| `evaluation.started` / `evaluation.finished` | `evaluation:<run>` | golden set and digest, providers, exclusions, estimate, policy digest, error |
| `judge.calibrated` | `judge:<provider>` | rubric digest, items, weighted kappa |
| `promotion.requested` / `promotion.decided` | `promotion:<id>` | family, provider, change, evidence, matched rules, requester, reason |
| `access.denied` | `<type>:<id>` | requested action, channel (http, mcp, a2a), source, matched rules, policy digest |
| `mcp.tool_call` | `<type>:<id>` | tool, delegation chain |
