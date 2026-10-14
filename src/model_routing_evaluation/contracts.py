"""Export published contracts: JSON Schemas for inputs/outputs and the OpenAPI document.

python -m model_routing_evaluation.contracts            # rewrite schemas/ and docs/api/
python -m model_routing_evaluation.contracts --check    # exit 1 if committed files are stale
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

from fastapi import FastAPI
from pydantic import BaseModel

from model_routing_evaluation.api.app import create_app
from model_routing_evaluation.application.commands import (
    CalibrationCommand,
    FeedbackCommand,
    PromotionCommand,
    PromotionDecision,
    RouteCommand,
    RouteResult,
    RunCommand,
)
from model_routing_evaluation.application.reports import FamilyReport
from model_routing_evaluation.domain.evaluation import (
    CalibrationResult,
    EvaluationRun,
    GoldenSet,
    Promotion,
)
from model_routing_evaluation.domain.models import (
    ComplianceProfile,
    DecisionRecord,
    LedgerEntry,
    RoutePlan,
)
from model_routing_evaluation.routing.config import RoutingConfig

MODELS: dict[str, type[BaseModel]] = {
    "route.request": RouteCommand,
    "route.response": RouteResult,
    "route-plan.response": RoutePlan,
    "feedback.request": FeedbackCommand,
    "evaluation-run.request": RunCommand,
    "evaluation-run.response": EvaluationRun,
    "judge-calibration.request": CalibrationCommand,
    "judge-calibration.response": CalibrationResult,
    "promotion.request": PromotionCommand,
    "promotion-decision.request": PromotionDecision,
    "promotion.response": Promotion,
    "family-report.response": FamilyReport,
    "decision-record": DecisionRecord,
    "ledger-entry": LedgerEntry,
    "golden-set": GoldenSet,
    "routing-config": RoutingConfig,
    "compliance-profile": ComplianceProfile,
}


async def _no_container() -> Any:  # pragma: no cover - never started
    raise RuntimeError("contract export does not start the application")


def documents() -> dict[str, dict[str, Any]]:
    docs: dict[str, dict[str, Any]] = {
        f"schemas/{name}.schema.json": {
            "$schema": "https://json-schema.org/draft/2020-12/schema",
            "$id": (
                "https://github.com/kogunlowo123/model-routing-evaluation/"
                f"schemas/{name}.schema.json"
            ),
            **model.model_json_schema(mode="validation"),
        }
        for name, model in MODELS.items()
    }
    app: FastAPI = create_app(_no_container)
    docs["docs/api/openapi.json"] = app.openapi()
    return docs


def render(document: dict[str, Any]) -> str:
    return json.dumps(document, indent=2, sort_keys=True) + "\n"


def main(argv: list[str] | None = None, root: Path | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    base = root or Path.cwd()
    stale: list[str] = []
    for relative, document in documents().items():
        path = base / relative
        content = render(document)
        if "--check" in args:
            if not path.exists() or path.read_text(encoding="utf-8") != content:
                stale.append(relative)
        else:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8", newline="\n")
    if stale:
        sys.stderr.write(
            "stale contracts (run python -m model_routing_evaluation.contracts): "
            + ", ".join(stale)
            + "\n"
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
