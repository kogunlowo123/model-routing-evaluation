"""Full lifecycle through the ASGI app: route, learn, evaluate, calibrate, promote, report."""

from __future__ import annotations

import time
from typing import Any

import pytest
from fastapi.testclient import TestClient

from tests.conftest import (
    AGENT,
    OTHER_AGENT,
    FakeIdp,
    FakeModelWorld,
    agent_headers,
    approver_headers,
    route_body,
    triage_inputs,
)

pytestmark = pytest.mark.e2e

TRIAGE_SYSTEM = (
    "Classify the customer support ticket into exactly one of these labels:\n"
    "billing, technical, account_access, shipping, cancellation, other.\n"
    "Reply with the label only, in lower case, with no punctuation.\n"
)


def wait_for_run(client: TestClient, run_id: str, headers: dict[str, str]) -> dict[str, Any]:
    for _ in range(200):
        body = client.get(f"/v1/evaluations/{run_id}", headers=headers).json()
        if body["run"]["status"] != "running":
            return body
        time.sleep(0.02)
    raise AssertionError("evaluation did not finish")


def test_route_returns_answer_and_decision(client: TestClient, agent_idp: FakeIdp) -> None:
    response = client.post("/v1/route", json=route_body(), headers=agent_headers(agent_idp))
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["provider"] in {"azure-gpt", "claude", "llama"}
    assert body["text"].startswith(f"[{body['provider']}]")
    assert body["family"] == "general"
    assert body["strategy"] == "learned"
    assert {c["provider"] for c in body["candidates"]} == {"azure-gpt", "claude", "llama"}
    assert all(c["utility"] is not None for c in body["candidates"])
    excluded = {e["provider"]: e["reason"] for e in body["excluded"]}
    assert excluded["phi"] == "not_in_pool"
    assert body["cost_usd"] > 0
    assert response.headers["X-Content-Type-Options"] == "nosniff"
    assert response.headers["X-Correlation-ID"]


def test_operations_endpoints(client: TestClient) -> None:
    assert client.get("/healthz").json() == {"status": "ok"}
    assert client.get("/readyz").json() == {"status": "ready"}
    metrics = client.get("/metrics")
    assert metrics.status_code == 200
    assert "mre_http_request_duration_seconds" in metrics.text


def test_learned_routing_converges_on_quality(
    client: TestClient, agent_idp: FakeIdp, world: FakeModelWorld
) -> None:
    """Llama always answers wrong in the simulated world; feedback steers traffic away."""
    headers = agent_headers(agent_idp)
    served: list[str] = []
    for i in range(60):
        prompt = f"Draft a reply to customer request number {i} about invoice timing."
        decision = client.post("/v1/route", json=route_body(prompt), headers=headers).json()
        score = 0.0 if decision["provider"] == "llama" else 1.0
        fb = client.post(
            "/v1/feedback",
            json={"decision_id": decision["decision_id"], "score": score},
            headers=headers,
        )
        assert fb.status_code == 200, fb.text
        served.append(decision["provider"])
    late = served[-20:]
    assert late.count("llama") <= 3
    report = client.get("/v1/reports/general", headers=headers).json()
    llama = next(p for p in report["providers"] if p["provider"] == "llama")
    best = max(report["providers"], key=lambda p: p["posterior_quality"])
    assert llama["posterior_quality"] < best["posterior_quality"]


def test_offline_evaluation_calibration_promotion_cycle(
    client: TestClient, agent_idp: FakeIdp, approver_idp: FakeIdp, world: FakeModelWorld
) -> None:
    headers = agent_headers(agent_idp)
    sets = client.get("/v1/golden-sets", headers=headers).json()["items"]
    assert {s["name"] for s in sets} == {
        "support-ticket-triage",
        "invoice-field-extraction",
        "incident-summary",
    }

    started = client.post(
        "/v1/evaluations",
        json={
            "golden_set": "support-ticket-triage",
            "providers": ["phi", "azure-gpt", "nova", "llama"],
            "pairwise": False,
        },
        headers=headers,
    )
    assert started.status_code == 202, started.text
    run = wait_for_run(client, started.json()["run_id"], headers)
    assert run["run"]["status"] == "completed", run["run"]
    summary = {p["provider"]: p for p in run["run"]["summary"]["providers"]}
    assert summary["azure-gpt"]["pass_rate"] == 1.0
    assert summary["llama"]["pass_rate"] == 0.0
    assert len(run["results"]) == 12 * 4

    # Llama is not in the classify pool; it fails the gate, so promotion is refused.
    refused = client.post(
        "/v1/promotions",
        json={
            "family": "classify",
            "provider": "llama",
            "run_id": run["run"]["run_id"],
            "justification": "Cheaper open-weight model for ticket triage.",
        },
        headers=headers,
    )
    assert refused.status_code == 422
    assert "pass rate" in str(refused.json()["details"]["gate"])

    # Make llama accurate and evaluate again: now it may be proposed, then approved.
    world.accuracy["llama"] = 1.0
    rerun = client.post(
        "/v1/evaluations",
        json={"golden_set": "support-ticket-triage", "providers": ["llama", "azure-gpt"]},
        headers=headers,
    )
    evidence = wait_for_run(client, rerun.json()["run_id"], headers)
    assert evidence["run"]["status"] == "completed"
    requested = client.post(
        "/v1/promotions",
        json={
            "family": "classify",
            "provider": "llama",
            "run_id": evidence["run"]["run_id"],
            "justification": "Open-weight model passed the triage golden set.",
        },
        headers=headers,
    )
    assert requested.status_code == 201, requested.text
    promotion = requested.json()
    assert promotion["status"] == "pending"
    assert promotion["evidence"]["pass_rate"] == 1.0

    # Agents cannot decide; an approver with the role can.
    assert (
        client.post(
            f"/v1/promotions/{promotion['promotion_id']}/decision",
            json={"approve": True, "reason": "self approval attempt"},
            headers=headers,
        ).status_code
        == 401
    )
    decided = client.post(
        f"/v1/promotions/{promotion['promotion_id']}/decision",
        json={"approve": True, "reason": "Evidence reviewed against the gate."},
        headers=approver_headers(approver_idp, "model-governance-approver"),
    )
    assert decided.status_code == 200, decided.text
    assert decided.json()["status"] == "approved"

    plan = client.post(
        "/v1/route/plan",
        json={"task": "classify", "messages": [{"role": "user", "content": triage_inputs()[0]}]},
        headers=headers,
    ).json()
    assert "llama" in [c["provider"] for c in plan["candidates"]]

    report = client.get("/v1/reports/classify?format=markdown", headers=headers)
    assert report.status_code == 200
    assert report.headers["content-type"].startswith("text/markdown")
    assert "| llama | yes |" in report.text


def test_pairwise_ranking_and_judge_calibration(
    client: TestClient, agent_idp: FakeIdp, world: FakeModelWorld
) -> None:
    headers = agent_headers(agent_idp)
    items = [
        {
            "prompt": f"incident {i}",
            "output": "Accurate summary." if i % 2 else "WRONG summary.",
            "human_score": 5 if i % 2 else 1,
        }
        for i in range(8)
    ]
    calibration = client.post(
        "/v1/judges/calibrations",
        json={
            "judge_provider": "claude",
            "rubric": "Score faithfulness and completeness of the incident summary.",
            "items": items,
        },
        headers=headers,
    )
    assert calibration.status_code == 201, calibration.text
    result = calibration.json()
    assert result["calibrated"] is True
    assert result["weighted_kappa"] == 1.0

    world.accuracy.update({"phi": 1.0, "nova": 0.0})
    started = client.post(
        "/v1/evaluations",
        json={"golden_set": "incident-summary", "providers": ["phi", "nova"], "pairwise": True},
        headers=headers,
    )
    assert started.status_code == 202, started.text
    run = wait_for_run(client, started.json()["run_id"], headers)
    assert run["run"]["status"] == "completed", run["run"]
    ranking = run["run"]["summary"]["ranking"]
    assert [r["provider"] for r in ranking] == ["phi", "nova"]
    assert ranking[0]["strength"] > ranking[1]["strength"]
    # Rubric differs from the calibrated one, so the judge counts as uncalibrated.
    assert run["run"]["summary"]["judge_calibrated"] is False


def test_ledger_and_other_agent_feedback(client: TestClient, agent_idp: FakeIdp) -> None:
    headers = agent_headers(agent_idp)
    decision = client.post("/v1/route", json=route_body(), headers=headers).json()
    other = client.post(
        "/v1/feedback",
        json={"decision_id": decision["decision_id"], "score": 1.0},
        headers=agent_headers(agent_idp, sub=OTHER_AGENT),
    )
    assert other.status_code == 403
    ledger = client.get("/v1/ledger?group_by=caller", headers=headers).json()
    assert ledger["totals"][0]["key"] == AGENT
    assert ledger["total_cost_usd"] == pytest.approx(decision["cost_usd"])
    providers = client.get("/v1/providers", headers=headers).json()["items"]
    assert {p["name"] for p in providers} == {
        "azure-gpt",
        "phi",
        "llama",
        "claude",
        "gemini",
        "nova",
    }
    assert all(p["circuit"] == "closed" for p in providers)
