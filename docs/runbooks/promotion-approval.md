# Runbook: routing-pool promotion approval

A promotion adds a provider to a task family's routing pool or removes one.
The requesting agent supplies evidence; a human with the
`model-governance-approver` role who is not the requester decides.

## 1. Produce evidence (requester)

```bash
RUN=$(curl -s $MRE/v1/evaluations -H "Authorization: Bearer $AGENT_TOKEN" \
  -H 'content-type: application/json' \
  -d '{"golden_set":"support-ticket-triage","providers":["llama","phi"],"pairwise":false}' | jq -r .run_id)
curl -s $MRE/v1/evaluations/$RUN -H "Authorization: Bearer $AGENT_TOKEN" | jq '.run.status, .run.summary.providers'
```

Gates per family (`configs/routing.yaml`, `promotion`): minimum cases,
minimum pass rate and, where `require_calibrated_judge` is true, a judge
calibrated for the rubric (`POST /v1/judges/calibrations`).

## 2. Request

```bash
curl -s $MRE/v1/promotions -H "Authorization: Bearer $AGENT_TOKEN" \
  -H 'content-type: application/json' \
  -d "{\"family\":\"classify\",\"provider\":\"llama\",\"action\":\"add\",\"run_id\":\"$RUN\",
       \"justification\":\"Passed the triage golden set at the gate.\"}"
```

A 422 with `details.gate` lists unmet gate conditions. A 201 with
`status: pending` is waiting for a decision.

## 3. Review (approver)

```bash
curl -s "$MRE/v1/promotions?status=pending" -H "Authorization: Bearer $AGENT_TOKEN" | jq
curl -s "$MRE/v1/reports/classify?format=markdown" -H "Authorization: Bearer $AGENT_TOKEN"
```

Check:

1. The evidence `golden_digest` matches the reviewed golden set version.
2. `pass_rate_ci95_low` is acceptable, not only the point estimate.
3. The provider's compliance profile (`GET /v1/providers`) suits the family's
   typical data; the compliance filter will still exclude it per request where
   it does not qualify.
4. Cost and latency in the report and the offline summary fit the family.

## 4. Decide

```bash
curl -s $MRE/v1/promotions/<promotion_id>/decision \
  -H "Authorization: Bearer $APPROVER_TOKEN" -H 'content-type: application/json' \
  -d '{"approve": true, "reason": "Evidence reviewed against the classify gate."}'
```

The service re-checks the evidence against the current pool before applying.
The requester cannot decide (403), a decided promotion cannot be decided
again (409), and every step is in the audit chain (`promotion.requested`,
`promotion.decided`).

## Removal

Use `"action": "remove"` with a justification; no run is required, but the
last provider in a pool cannot be removed.
