"""Load profile for a deployed service.

Run against a staging deployment with real or recorded model endpoints, never production:

    MRE_LOAD_AGENT_TOKEN=<agent credential> \
    locust -f tests/performance/locustfile.py --host https://routing.staging.corp.internal \
           --users 100 --spawn-rate 10 --run-time 10m --headless

The mix weights routing far above planning and reporting, matching agent
traffic: every agent call routes, a few inspect plans, dashboards read reports.
Each routed request incurs real model spend; size the run with the cost ledger.
"""

from __future__ import annotations

import os

from locust import HttpUser, between, task

TOKEN = os.environ.get("MRE_LOAD_AGENT_TOKEN", "")
HEADERS = {"Authorization": f"Bearer {TOKEN}"}


class AgentTraffic(HttpUser):
    wait_time = between(0.1, 0.5)

    @task(20)
    def route_classify(self) -> None:
        self.client.post(
            "/v1/route",
            json={
                "task": "classify",
                "max_tokens": 16,
                "messages": [
                    {"role": "system", "content": "Reply with one label: billing or technical."},
                    {"role": "user", "content": "The export API times out after 30 seconds."},
                ],
            },
            headers=HEADERS,
            name="/v1/route [classify]",
        )

    @task(5)
    def plan(self) -> None:
        self.client.post(
            "/v1/route/plan",
            json={
                "task": "summarize",
                "constraints": {"residency": ["US"]},
                "messages": [{"role": "user", "content": "Summarise the incident timeline."}],
            },
            headers=HEADERS,
            name="/v1/route/plan",
        )

    @task(1)
    def report(self) -> None:
        self.client.get("/v1/reports/classify", headers=HEADERS, name="/v1/reports/{family}")
