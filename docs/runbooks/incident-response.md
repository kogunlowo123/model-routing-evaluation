# Runbook: incident response

Severity follows the alert label (`page` or `ticket`). Every action below is
recorded in the audit chain; collect the correlation IDs of affected requests
(`X-Correlation-ID`) for the incident record.

## Elevated 5xx

Alerts: `MreErrorBudgetBurnFast`, `MreErrorBudgetBurnSlow`.

1. `kubectl -n model-routing get pods`; `kubectl logs` for exceptions.
2. `curl -sf $MRE/readyz`: a 503 means the database is unreachable; follow
   [disaster recovery](disaster-recovery.md) if the server is down.
3. Distinguish vendor failures from service failures: 503 responses with
   `error: upstream_unavailable` and `route.failed` audit events are vendor
   outages, see [provider outage](provider-outage.md).
4. If the identity fabric is down, every agent call fails closed with 503.
   Restore the fabric; do not switch to local verification without a change
   record.
5. A failing audit write fails requests by design. Check database disk and
   permissions on `audit_events`.

## Route failures

Alert: `MreRouteFailureRatioHigh`.

- `reason="no_eligible_provider"`: callers ask for constraints no pool member
  meets. Use `POST /v1/route/plan` with the caller's body to see exclusions.
  Common causes: a residency list that excludes every provider, a cost ceiling
  below every estimate, or a promotion that removed the last compliant
  provider. Do not relax compliance facts to make traffic flow.
- `reason="providers_unavailable"`: every planned provider failed; see
  [provider outage](provider-outage.md).

## Guard blocks

Alert: `MreGuardBlockSpike`.

- `stage="input"`: injection or credentials in requests. Identify the callers
  from `route.blocked` audit events (actor field) and contact their owners; a
  compromised agent should be suspended in agent-identity-fabric.
- `stage="output"`: a provider returned credential material. Treat as a
  potential data exposure at the vendor: preserve the audit events, open a
  vendor case, and consider removing the provider from affected pools
  ([promotion approval](promotion-approval.md), action `remove`).

## Replicas down

Alert: `MreNotReady`. Check node pressure, image pull errors and
`kubectl describe pod`. The PDB keeps two replicas during voluntary
disruption; if fewer remain, scale the deployment and check the HPA.

## Suspected tampering

1. `GET /v1/audit/verify` with an auditor token; any chain with
   `intact: false` names the first bad sequence.
2. Preserve a database snapshot before any change.
3. Rotate the audit key (new Key Vault secret
   `model-routing-evaluation-audit-key-<new kid>`, update
   `secrets.auditKeyFile`, roll the deployment). Old chains remain verifiable
   with the old public key.

## Evidence

For each incident capture: alert, time window, affected families and
providers (`mre_route_decisions_total`), audit events, ledger impact
(`GET /v1/ledger?group_by=provider&days=1`) and the corrective change.
