# Runbook: provider outage

## How the service reacts

- Each request tries its planned providers in order with retries (jittered
  backoff) for retryable vendor errors (408, 409, 425, 429, 5xx, 529, AWS
  throttling).
- After five consecutive failures a provider's circuit opens for 30 seconds;
  the router skips it and a single probe closes it again.
- Every failed provider records quality 0 for its arm, so the learned router
  moves traffic away until it recovers.

## Diagnose

```bash
curl -s $MRE/v1/providers -H "Authorization: Bearer $TOKEN" | jq '.items[] | {name, circuit}'
```

Prometheus:

```
sum by (provider, kind) (rate(mre_model_latency_seconds_count[5m]))
sum by (family, reason) (rate(mre_route_failures_total[5m]))
```

Compare with the vendor status page. Check egress: NetworkPolicy allows 443
only; Azure OpenAI and Key Vault use private endpoints and private DNS.

## Act

- Single provider down, others healthy: no action; failover and learning
  handle it. Confirm `failed_over: true` on responses and traffic shifting in
  the dashboard.
- A family whose whole pool is down: callers receive 503. If the outage is
  long, request a temporary pool addition with existing evaluation evidence
  ([promotion approval](promotion-approval.md)); compliance filters still
  apply to the new member.
- Credential problems (401/403 from the vendor): rotate the key in Key Vault;
  External Secrets refreshes hourly; force with
  `kubectl -n model-routing annotate externalsecret model-routing-evaluation-provider-keys force-sync=$(date +%s) --overwrite`
  then `kubectl -n model-routing rollout restart deployment/routing-model-routing-evaluation`.
- Bedrock (Nova) `AccessDenied`: check the projected token audience
  (`providers.bedrock.tokenAudience`) and the IAM role trust policy subject.

## Shadow errors

Alert: `MreShadowErrorRatioHigh`. Shadow providers failing do not affect
callers, but their comparisons stop producing evidence. If the shadow
provider is down, lower the family's `shadow.rate` to 0 in
`configs/routing.yaml` through a reviewed change, or wait for recovery.
If judges are failing (`detail` starting with `judge:` in
`shadow.compared` events), check the judge providers listed in
`shadow.judge_providers`.
