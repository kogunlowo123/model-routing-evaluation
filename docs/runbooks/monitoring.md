# Runbook: monitoring

Alert rules: `configs/prometheus/alerts.yaml`. Dashboard:
`configs/grafana/model-routing-evaluation.json`.

## Key signals

| Signal | Query | Healthy |
|---|---|---|
| Request errors | `mre:request_errors:ratio_5m` | Below 0.1% |
| Route failures | `mre:route_failures:ratio_15m` | Near 0; non-zero values carry a `reason` label |
| Traffic by provider | `sum by (family, provider) (rate(mre_route_decisions_total[5m]))` | Shifts slowly as the bandit learns |
| Exclusions | `sum by (family, reason) (rate(mre_compliance_exclusions_total[1h]))` | Stable; a new reason usually means a configuration change |
| Spend | `sum by (family, provider, kind) (increase(mre_model_cost_usd_total[1h]))` | Within the FinOps forecast |
| Guard blocks | `sum by (stage) (rate(mre_guard_blocks_total[10m]))` | Low and stable |
| Shadow outcomes | `sum by (family, outcome) (increase(mre_shadow_comparisons_total[1d]))` | Few `error`; `budget_exhausted` only late in the UTC day |
| Authorization | `sum by (source, decision) (rate(mre_authorization_decisions_total[10m]))` | Denials low |

## Latency

`MreRouteOverheadHigh` fires when the p99 of `/v1/route` minus the p99 model
latency exceeds 150 ms. Model latency itself is tracked per provider in
`mre_model_latency_seconds` and is the vendor's, not the service's.

1. Check database latency (Azure Monitor for the Flexible Server) and
   connection saturation.
2. Check CPU throttling of the pods; the HPA scales on CPU.
3. Check `mre_shadow_in_flight`: shadow comparisons share the event loop.
   Lower `families.<name>.shadow.rate` or the shadow budget if it is high.

For slow vendors, the learned router already shifts traffic according to the
family latency weight; see [provider outage](provider-outage.md) for
persistent problems.

## Learning health

Review `GET /v1/reports/<family>?format=markdown` weekly per family:

- `Posterior quality` intervals should narrow over time. Wide intervals on a
  high-traffic provider mean little feedback is arriving; ask callers to send
  `POST /v1/feedback` or supply `response_schema`.
- `Shadow win` above 0.5 with many comparisons suggests a promotion
  candidate; confirm with an offline run.
- `Offline pass` should be recent; schedule runs after any vendor model update.
