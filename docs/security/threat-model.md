# Threat model

## Assets

| Asset | Impact if compromised |
|---|---|
| Request and response content in flight | Disclosure of personal data, PHI or trade secrets to an unapproved vendor or caller |
| Compliance profiles and routing pools | Data routed to a provider without residency approval or a BAA |
| Provider API keys and workload identities | Spend and data access in the organisation's vendor accounts |
| Bandit statistics and feedback | Traffic steered to a worse or attacker-preferred provider |
| Evaluation evidence and judge calibrations | Unjustified promotion of a provider into a pool |
| Cost ledger | Hidden spend, misattributed charges |
| Audit chains and audit signing key | Loss of accountability; forged history |
| Approver identities | Unauthorised pool changes |

## Trust boundaries

1. Agents and applications to the API, `/mcp` and `/a2a` (untrusted callers
   with bearer credentials).
2. Service to agent-identity-fabric and the enterprise IdP JWKS.
3. Service to six model vendors (untrusted outputs; data leaves the
   organisation).
4. Service to PostgreSQL, Key Vault and the cross-cloud AWS role.
5. Model output to the judge (output is untrusted material, never instructions).

## STRIDE

Security tests are in `tests/security/test_routing_attacks.py` unless another
file is named.

| ID | Threat | Category | Control | Test |
|---|---|---|---|---|
| T1 | Caller sets constraints that are ignored and data reaches an out-of-region provider | Information disclosure | Hard compliance filter before ordering; same filter for shadows and judges | `test_residency_never_violated` |
| T2 | RESTRICTED data sent to a provider without a BAA | Information disclosure | BAA required for RESTRICTED; classification ceilings; purpose of use required by policy | `test_baa_required_for_restricted` |
| T3 | Prompt injection in user or retrieved content | Tampering / elevation | `ContentGuard` injection scoring per trust level; external content spotlighted | `test_prompt_injection_blocked` |
| T4 | Model returns credentials or secrets | Information disclosure | Output secret scanning; answer withheld (502) and charged; quality 0 | `test_credential_in_output_withheld` |
| T5 | Output instructs the judge to give it a high score | Tampering | Spotlighting, injection scan of judged material, judge independence, position swap | `test_judge_manipulation_detected` |
| T6 | Caller-supplied response schema with catastrophic regex or remote references | Denial of service | Size and depth limits; `pattern`, `patternProperties`, `format` and reference keywords refused | `test_caller_schema_redos_rejected` |
| T7 | Feedback replayed to bias the bandit | Tampering | One feedback per decision (409) | `test_feedback_replay_rejected` |
| T8 | Another agent rates a decision it did not make | Spoofing / tampering | Feedback only from the decision's caller (403) | `test_feedback_by_other_agent_rejected` |
| T9 | Requester approves their own pool change | Elevation | Two-person rule enforced in the service | `test_requester_cannot_approve_own_promotion` |
| T10 | Agent credential used to approve a promotion | Elevation | Decisions need an enterprise OIDC token with the approver role | `test_agent_token_cannot_approve` |
| T11 | Forged or foreign-issuer bearer token | Spoofing | Asymmetric algorithms only, issuer, audience, expiry and kid checks; fabric validation | `test_forged_token_rejected` |
| T12 | Identity fabric down: service allows calls | Elevation | Fail closed (503); no allow-all mode | `test_fabric_outage_fails_closed` |
| T13 | Code path calls a model without the compliance plan | Information disclosure | `PlannedRouter` returns an empty plan for unplanned requests | `test_unplanned_model_call_refused` |
| T14 | Audit rows altered or deleted | Repudiation | Append-only triggers, hash chain, Ed25519 signatures, verify endpoint | `test_audit_tamper_detected` |
| T15 | Audit write fails silently | Repudiation | Audit failure fails the request | `tests/chaos/test_dependency_failures.py` |
| T16 | Vendor outage or slow vendor exhausts the service | Denial of service | Retries with jitter, circuit breakers, failover, timeouts | `tests/chaos/test_dependency_failures.py` |
| T17 | Shadow or evaluation spend runs away | Denial of wallet | Daily shadow budget; evaluation cost estimate denied above budget by policy | `tests/unit/test_services.py` |

## MITRE ATT&CK (Enterprise)

| Technique | Mitigation |
|---|---|
| T1078.004 Valid Accounts: Cloud Accounts | Workload identity only; no static cloud keys; Bedrock via federated role limited to Nova |
| T1528 Steal Application Access Token | Short-lived fabric credentials; provider keys only in Key Vault, synced by External Secrets, never logged |
| T1552 Unsecured Credentials | Secret scanning on inputs and outputs; scrubbed logs |
| T1565 Data Manipulation | Append-only audit, signed chains, policy digests on decisions |
| T1562.008 Impair Defenses: Disable Cloud Logs | Audit failure blocks the request; replicas-down and burn-rate alerts |
| T1496 Resource Hijacking | Shadow budget, evaluation budget, spend anomaly alert |

## MITRE ATLAS

| Technique | Relevance | Control |
|---|---|---|
| AML.T0051 LLM Prompt Injection | Direct and indirect injection in routed content and judged outputs | `ContentGuard`, spotlighting, judge manipulation check |
| AML.T0054 LLM Jailbreak | Mode-switch and role-reassignment attempts | Injection signals block at the trust-level threshold |
| AML.T0034 Cost Harvesting | Attacker drives expensive models or evaluations | Cost ceilings per request, utility cost weight, shadow and evaluation budgets, per-caller ledger |
| AML.T0029 Denial of ML Service | Flooding or vendor outage | HPA, breakers, failover, ingress rate limiting, 5xx burn alerts |
| AML.T0040 ML Model Inference API Access | Unauthorised use of vendor models through this service | Every call authorized per action through the fabric |
| AML.T0048 External Harms | Harmful or leaked output reaching users | Output guard, data minimisation, audit |

## OWASP Top 10 for LLM Applications (2025)

| Risk | Control |
|---|---|
| LLM01 Prompt Injection | Input guard with trust levels; external content isolation; judges treat outputs as data |
| LLM02 Sensitive Information Disclosure | Hard compliance filters; PII redaction both ways; secret blocking; digests instead of content |
| LLM03 Supply Chain | Pinned agentic-core tag and lockfile; pip-audit; SBOM, Trivy and Grype in CI; models pinned by deployment in configuration |
| LLM04 Data and Model Poisoning | Golden sets versioned and digested; feedback once per decision by the caller; offline prior weight below online evidence |
| LLM05 Improper Output Handling | Output guard; optional JSON Schema validation reported as `schema_valid` |
| LLM06 Excessive Agency | The service only routes and evaluates; pool changes need human approval |
| LLM07 System Prompt Leakage | System prompt exfiltration signals in the injection detector |
| LLM08 Vector and Embedding Weaknesses | Not applicable: no retrieval store in this service |
| LLM09 Misinformation | Evaluation harness, calibrated judges, shadow comparisons, quality-weighted routing |
| LLM10 Unbounded Consumption | Token caps per request, cost ceilings, budgets, body size limits, autoscaling |

## OWASP API Security Top 10 (2023)

| Risk | Control |
|---|---|
| API1 Broken Object Level Authorization | Feedback restricted to the decision's caller; evaluation and report reads authorized per action |
| API2 Broken Authentication | Fabric validation or local JWKS verification with strict claims; asymmetric algorithms only |
| API3 Broken Object Property Level Authorization | `extra="forbid"` on every command; responses omit prompts and secrets |
| API4 Unrestricted Resource Consumption | Message and schema limits, report windows bounded, list endpoints bounded, budgets |
| API5 Broken Function Level Authorization | Distinct actions per endpoint; promotion decisions and audit verification need human roles |
| API6 Unrestricted Access to Sensitive Business Flows | Promotion evidence gate and two-person rule |
| API7 Server Side Request Forgery | Vendor endpoints come only from configuration; callers cannot supply URLs; schema references refused |
| API8 Security Misconfiguration | Start-up fails on unset configuration; Helm `required`; production refuses ephemeral keys; restricted Pod Security |
| API9 Improper Inventory Management | OpenAPI and schemas generated and drift-checked in CI |
| API10 Unsafe Consumption of APIs | Vendor responses parsed defensively by agentic-core adapters; malformed responses are provider errors |

## Risk register

| ID | Risk | Likelihood | Impact | Owner | Treatment | Residual |
|---|---|---|---|---|---|---|
| R1 | Compliance facts misconfigured (BAA flag true without a signed BAA) | Low | Critical | Legal and procurement | Explicit Helm values with no defaults; change review on values; quarterly attestation | Low |
| R2 | Vendor changes data handling terms | Medium | High | Procurement | Contract review triggers a compliance profile change and redeploy | Medium |
| R3 | Judge bias inflates a provider's evaluation | Medium | Medium | AI governance | Calibration gate, independence, position swap, human approval | Low |
| R4 | Feedback poisoning by a compromised agent | Low | Medium | Platform | Once per decision, per caller, audited; anomaly review in reports | Low |
| R5 | Vendor outage across the pool | Medium | High | Platform | Failover, breakers, multi-vendor pools, provider-outage runbook | Medium |
| R6 | Runaway spend | Medium | High | FinOps | Ceilings, budgets, ledger, spend alert | Low |
| R7 | PostgreSQL outage | Low | High | Platform | Zone-redundant HA, readiness probe, PITR | Low |
| R8 | Audit signing key exfiltration | Low | High | Security operations | Key Vault, RBAC, rotation runbook in incident response | Low |
