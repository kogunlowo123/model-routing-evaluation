# Runbook: cost spike

## Spend anomaly

Alerts: `MreSpendAnomaly` (hourly spend above three times its seven-day
hourly average) and `MreShadowBudgetExhaustedEarly` (shadow budget used up
before 12:00 UTC).

## Find the source

```bash
curl -s "$MRE/v1/ledger?group_by=kind&days=1"     -H "Authorization: Bearer $TOKEN" | jq
curl -s "$MRE/v1/ledger?group_by=caller&days=1"   -H "Authorization: Bearer $TOKEN" | jq '.totals | sort_by(-.cost_usd)[:10]'
curl -s "$MRE/v1/ledger?group_by=provider&days=1" -H "Authorization: Bearer $TOKEN" | jq
```

Prometheus: `topk(5, sum by (family, provider, kind) (increase(mre_model_cost_usd_total[1h])))`.

## Act by kind

- `route`: a caller's volume changed or the bandit moved traffic to an
  expensive provider. Check the family report: if quality justifies it, the
  family weights are doing their job; if not, raise the family `cost` weight
  through a reviewed change to `configs/routing.yaml`. Callers can set
  `constraints.max_cost_usd` per request. A runaway agent should be suspended
  in agent-identity-fabric.
- `shadow`: lower `MRE_SHADOW_DAILY_BUDGET_USD` (Helm
  `mre.shadowDailyBudgetUsd`) or the family `shadow.rate`. Setting the budget
  to 0 stops shadows immediately after the next deploy.
- `evaluation` / `judge`: list recent runs from `evaluation.started` audit
  events; the governance policy caps the estimate per run
  (`deny-evaluation-over-budget`). Lower the cap in
  `policies/model-governance.yaml` through review if needed.

## Prices

Costs are computed from configured contract prices. If a vendor changed its
rates, update the price values (Helm `providers.*.inputUsdPerMtok` /
`outputUsdPerMtok`) and redeploy; historical ledger rows keep the price that
applied when the call was made.
