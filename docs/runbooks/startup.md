# Runbook: first deployment and startup

Reference cloud: Azure (AKS). AWS and GCP follow the same steps with their
Terraform folder and secret manager.

## Prerequisites

- AKS 1.27 or later with the OIDC issuer and workload identity enabled,
  External Secrets Operator, and a `ClusterSecretStore` named
  `platform-secrets` bound to the Key Vault created below.
- A VNet with a subnet delegated to PostgreSQL Flexible Server, a subnet for
  private endpoints, and private DNS zones for PostgreSQL, Key Vault and
  Azure OpenAI.
- agent-identity-fabric reachable from the cluster (or a JWKS-publishing
  agent token issuer for local verification mode).
- An Entra ID app registration exposing roles `model-governance-approver` and
  `model-governance-auditor`.
- Signed agreements for each vendor deployment, so the BAA and retention
  flags can be set truthfully.

## 1. Provision Azure resources

```bash
cd deployments/terraform/azure
terraform init                       # configure an encrypted remote backend first
terraform apply -var-file=<environment>.tfvars
terraform output
```

Record `workload_identity_client_id`, `key_vault_uri`,
`azure_openai_endpoint`, `azure_openai_deployment` and `region`.

For Nova on Bedrock, register the AKS OIDC issuer as an IAM OIDC provider in
AWS and apply `deployments/terraform/aws` with `oidc_provider_arn` and
`oidc_issuer_url` pointing at it; record `service_role_arn`.

## 2. Create secrets

```bash
VAULT=<key vault name>
openssl genpkey -algorithm ed25519 -out audit-2026-10.pem
az keyvault secret set --vault-name "$VAULT" \
  --name model-routing-evaluation-audit-key-2026-10 --file audit-2026-10.pem
shred -u audit-2026-10.pem

for name in azure-openai-api-key azure-ai-foundry-api-key anthropic-api-key gemini-api-key; do
  az keyvault secret set --vault-name "$VAULT" \
    --name "model-routing-evaluation-$name" --file "/run/secrets/$name"
done
```

The database URL secret (`model-routing-evaluation-database-url`) is written
by Terraform.

## 3. Namespace and secret sync

```bash
kubectl apply -k deployments/kubernetes
kubectl -n model-routing get externalsecret     # all READY=True
```

## 4. Install the chart

Create `<environment>.yaml` with every value marked REQUIRED in
`deployments/helm/model-routing-evaluation/values.yaml`: `mre.publicUrl`,
`mre.fabricUrl` (or all `mre.agentAuth` values), `mre.approver.*`, every
`providers.*` value including explicit `baa` and `zeroRetention` booleans
and contracted prices, `providers.bedrock.roleArn`, and
`secrets.auditKeyFile: audit-2026-10.pem`. Add the service-account annotation
`azure.workload.identity/client-id` and pod label
`azure.workload.identity/use: "true"`.

```bash
helm upgrade --install routing oci://ghcr.io/kogunlowo123/charts/model-routing-evaluation \
  --version 0.1.0 -n model-routing -f <environment>.yaml --wait
```

The chart refuses to render while any required value is missing.

## 5. Verify

```bash
kubectl -n model-routing rollout status deployment/routing-model-routing-evaluation
kubectl -n model-routing port-forward svc/routing-model-routing-evaluation 8080:80
curl -sf localhost:8080/readyz
curl -s localhost:8080/.well-known/agent-card.json | jq '.url, [.skills[].id]'
TOKEN=<agent credential with model-routing:plan>
curl -s localhost:8080/v1/route/plan -H "Authorization: Bearer $TOKEN" \
  -H 'content-type: application/json' \
  -d '{"task":"classify","messages":[{"role":"user","content":"Card charged twice"}]}' \
  | jq '.candidates[].provider, .excluded'
```

Expected: `{"status":"ready"}`, the agent card URL equals
`<publicUrl>/a2a`, and the plan lists the classify pool with exclusion
reasons for every other provider. Prometheus target
`model-routing-evaluation` is up and the Grafana dashboard
`configs/grafana/model-routing-evaluation.json` shows traffic.

## 6. Establish evaluation baselines

Run each golden set against its family pool and keep the run IDs; they are
the evidence for later promotions.

```bash
for set in support-ticket-triage invoice-field-extraction incident-summary; do
  curl -s localhost:8080/v1/evaluations -H "Authorization: Bearer $TOKEN" \
    -H 'content-type: application/json' -d "{\"golden_set\":\"$set\"}" | jq -r .run_id
done
```

Before enabling shadow traffic (`mre.shadowDailyBudgetUsd` above 0) or
relying on judge-graded families, calibrate each judge with human-scored
items (`POST /v1/judges/calibrations`).
