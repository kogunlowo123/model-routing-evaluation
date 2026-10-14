# Configuration reference

The service refuses to start with incomplete configuration: missing `MRE_*`
settings, any unset `${NAME}` reference in `configs/providers.yaml` or
`configs/compliance.yaml`, a family naming an unknown provider, or a golden
set naming an unknown family.

## Service settings (`MRE_*`)

Defined in `src/model_routing_evaluation/config.py`.

| Variable | Required | Default | Helm value | Description |
|---|---|---|---|---|
| `MRE_ENVIRONMENT` | no | `production` | `mre.environment` | `production` forbids ephemeral audit keys |
| `MRE_PUBLIC_URL` | yes | | `mre.publicUrl` | External base URL; the A2A card advertises `<url>/a2a` |
| `MRE_DATABASE_URL` | yes | | secret `secrets.database` key `DATABASE_URL` | SQLAlchemy async URL (`postgresql+asyncpg://...`) |
| `MRE_PROVIDERS_FILE` | no | `configs/providers.yaml` | image | Provider adapters and prices |
| `MRE_COMPLIANCE_FILE` | no | `configs/compliance.yaml` | image | Residency and contract facts |
| `MRE_ROUTING_FILE` | no | `configs/routing.yaml` | image | Task families |
| `MRE_GOLDEN_SETS_DIR` | no | `configs/golden-sets` | image | Golden sets |
| `MRE_GOVERNANCE_POLICY_FILE` | no | `policies/model-governance.yaml` | image | Governance policy |
| `MRE_ACCESS_POLICY_FILE` | no | `policies/agent-access.yaml` | image | Local-mode access policy |
| `MRE_AUDIT_KEY_FILE` | yes* | | `secrets.auditKey`, `secrets.auditKeyFile` | Ed25519 PEM; the file stem is the key id |
| `MRE_AUDIT_CHAIN_ID` | no | host name | pod name (downward API) | One audit chain per replica |
| `MRE_DEV_EPHEMERAL_KEYS` | no | `false` | not exposed | In-memory audit key, non-production only (replaces *) |
| `MRE_FABRIC_URL` | one of** | | `mre.fabricUrl` | agent-identity-fabric base URL |
| `MRE_AGENT_ISSUER` | one of** | | `mre.agentAuth.issuer` | Local mode: agent token issuer |
| `MRE_AGENT_AUDIENCE` | one of** | | `mre.agentAuth.audience` | Local mode: audience |
| `MRE_AGENT_JWKS_URL` | one of** | | `mre.agentAuth.jwksUrl` | Local mode: issuer JWKS |
| `MRE_APPROVER_ISSUER` | yes | | `mre.approver.issuer` | Enterprise OIDC issuer for approvers and auditors |
| `MRE_APPROVER_AUDIENCE` | yes | | `mre.approver.audience` | Audience of approver tokens |
| `MRE_APPROVER_JWKS_URL` | yes | | `mre.approver.jwksUrl` | Enterprise IdP JWKS |
| `MRE_APPROVER_ROLES_CLAIM` | no | `roles` | `mre.approver.rolesClaim` | Claim carrying roles |
| `MRE_APPROVER_ROLE` | no | `model-governance-approver` | `mre.approver.approverRole` | Role that decides promotions |
| `MRE_AUDITOR_ROLE` | no | `model-governance-auditor` | `mre.approver.auditorRole` | Role that verifies audit chains |
| `MRE_SHADOW_DAILY_BUDGET_USD` | no | `0` | `mre.shadowDailyBudgetUsd` | Shadow plus shadow-judge spend per UTC day; 0 disables shadows |
| `MRE_EVALUATION_CONCURRENCY` | no | `4` | `mre.evaluationConcurrency` | Parallel model calls per offline run (1 to 64) |
| `MRE_MIN_JUDGE_KAPPA` | no | `0.6` | `mre.minJudgeKappa` | Quadratic-weighted kappa a calibration needs |
| `MRE_MIN_CALIBRATION_ITEMS` | no | `30` | `mre.minCalibrationItems` | Items a calibration needs (at least 5) |
| `MRE_MCP_ALLOWED_HOSTS` | no | `[]` | `mre.mcpAllowedHosts` | JSON list of Host headers accepted on `/mcp`; empty disables the check |
| `MRE_LOG_LEVEL` | no | `INFO` | `mre.logLevel` | |
| `MRE_OTLP_ENDPOINT` | no | | `mre.otlpEndpoint` | OTLP gRPC endpoint for traces |
| `MRE_HOST` / `MRE_PORT` | no | `127.0.0.1` / `8080` | image sets `0.0.0.0` | Bind address |
| `MRE_FORWARDED_ALLOW_IPS` | no | `127.0.0.1` | chart | Proxies trusted for `X-Forwarded-*` |

\* Unless `MRE_DEV_EPHEMERAL_KEYS=true` outside production.
\** Set `MRE_FABRIC_URL`, or all three `MRE_AGENT_*` values.

## Provider variables (`configs/providers.yaml`)

| Variable | Provider | Helm value |
|---|---|---|
| `AZURE_OPENAI_ENDPOINT` | azure-gpt | `providers.azureOpenAI.endpoint` |
| `AZURE_OPENAI_DEPLOYMENT` | azure-gpt (deployment and model id) | `providers.azureOpenAI.deployment` |
| `AZURE_OPENAI_REGION` | azure-gpt | `providers.azureOpenAI.region` |
| `AZURE_OPENAI_API_KEY` | azure-gpt | secret `secrets.providerKeys` |
| `AZURE_GPT_INPUT_USD_PER_MTOK`, `AZURE_GPT_OUTPUT_USD_PER_MTOK` | azure-gpt | `providers.azureOpenAI.inputUsdPerMtok`, `outputUsdPerMtok` |
| `AZURE_AI_FOUNDRY_ENDPOINT` | phi, llama | `providers.azureAIFoundry.endpoint` |
| `AZURE_AI_FOUNDRY_REGION` | phi, llama | `providers.azureAIFoundry.region` |
| `AZURE_AI_FOUNDRY_API_KEY` | phi, llama | secret `secrets.providerKeys` |
| `PHI_INPUT_USD_PER_MTOK`, `PHI_OUTPUT_USD_PER_MTOK` | phi | `providers.azureAIFoundry.phiInputUsdPerMtok`, `phiOutputUsdPerMtok` |
| `LLAMA_INPUT_USD_PER_MTOK`, `LLAMA_OUTPUT_USD_PER_MTOK` | llama | `providers.azureAIFoundry.llamaInputUsdPerMtok`, `llamaOutputUsdPerMtok` |
| `ANTHROPIC_API_KEY` | claude | secret `secrets.providerKeys` |
| `CLAUDE_INPUT_USD_PER_MTOK`, `CLAUDE_OUTPUT_USD_PER_MTOK` | claude | `providers.anthropic.inputUsdPerMtok`, `outputUsdPerMtok` |
| `GEMINI_API_KEY` | gemini | secret `secrets.providerKeys` |
| `GEMINI_INPUT_USD_PER_MTOK`, `GEMINI_OUTPUT_USD_PER_MTOK` | gemini | `providers.gemini.inputUsdPerMtok`, `outputUsdPerMtok` |
| `AWS_REGION` | nova | `providers.bedrock.awsRegion` |
| `NOVA_INPUT_USD_PER_MTOK`, `NOVA_OUTPUT_USD_PER_MTOK` | nova | `providers.bedrock.inputUsdPerMtok`, `outputUsdPerMtok` |
| `AWS_ROLE_ARN`, `AWS_WEB_IDENTITY_TOKEN_FILE` | nova (cross-cloud) | set by the chart when `providers.bedrock.roleArn` is set |

Prices are contracted USD per million tokens. They are configuration because
rates differ by customer and change over time; nothing in the code assumes a
price.

## Compliance variables (`configs/compliance.yaml`)

| Variable | Meaning | Helm value |
|---|---|---|
| `AZURE_OPENAI_JURISDICTION`, `AZURE_OPENAI_BAA`, `AZURE_OPENAI_ZERO_RETENTION` | azure-gpt | `providers.azureOpenAI.jurisdiction`, `baa`, `zeroRetention` |
| `AZURE_AI_FOUNDRY_JURISDICTION`, `AZURE_AI_FOUNDRY_BAA`, `AZURE_AI_FOUNDRY_ZERO_RETENTION` | phi, llama | `providers.azureAIFoundry.*` |
| `ANTHROPIC_JURISDICTION`, `ANTHROPIC_REGION`, `ANTHROPIC_BAA`, `ANTHROPIC_ZERO_RETENTION` | claude | `providers.anthropic.*` |
| `GEMINI_JURISDICTION`, `GEMINI_REGION`, `GEMINI_BAA`, `GEMINI_ZERO_RETENTION` | gemini | `providers.gemini.*` |
| `BEDROCK_JURISDICTION`, `BEDROCK_BAA`, `BEDROCK_ZERO_RETENTION` | nova (region from `AWS_REGION`) | `providers.bedrock.*` |

Set `*_BAA=true` only where a signed BAA covers that deployment, and
`*_ZERO_RETENTION=true` only where the agreement says the vendor retains
nothing. The chart rejects unset booleans.

## Files

| File | Owner | Purpose |
|---|---|---|
| `configs/providers.yaml` | Platform | Adapters, approved classification ceilings, tasks served |
| `configs/compliance.yaml` | Legal and procurement (values) | Hard-filter facts |
| `configs/routing.yaml` | Platform and AI governance | Families: weights, targets, pools, shadow, promotion gates |
| `configs/golden-sets/*.yaml` | Task owners | Versioned evaluation sets |
| `policies/model-governance.yaml` | AI governance | Routing, evaluation and pool-change rules |
| `policies/agent-access.yaml` | Security | Local-mode agent access |
