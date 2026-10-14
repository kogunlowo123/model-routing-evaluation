"""Judge calibration against human scores.

A judge is only trusted for gating decisions (promotions) after it has been
measured against human ratings of the same rubric: quadratic-weighted kappa
must reach the configured threshold on at least ``min_items`` items. The
isotonic map learned here converts raw judge scores into expected human
scores whenever that judge grades with that rubric.
"""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime

from agentic_core.domain.classification import DataClassification
from agentic_core.domain.identifiers import new_id
from agentic_core.domain.provenance import content_digest
from agentic_core.models.types import ModelResponse

from model_routing_evaluation.domain.evaluation import CalibrationItem, CalibrationResult
from model_routing_evaluation.evaluation.judge import LlmJudge
from model_routing_evaluation.evaluation.statistics import (
    isotonic_fit,
    quadratic_weighted_kappa,
    spearman,
)


@dataclass(frozen=True)
class CalibrationOutcome:
    result: CalibrationResult
    responses: tuple[ModelResponse, ...]


async def calibrate(
    judge: LlmJudge,
    *,
    judge_provider: str,
    rubric: str,
    items: Sequence[CalibrationItem],
    classification: DataClassification,
    min_kappa: float,
    min_items: int,
    created_by: str,
    now: datetime,
    concurrency: int = 4,
) -> CalibrationOutcome:
    gate = asyncio.Semaphore(concurrency)

    async def one(item: CalibrationItem) -> tuple[int, tuple[ModelResponse, ...]]:
        async with gate:
            verdict = await judge.score(
                rubric=rubric,
                prompt=item.prompt,
                output=item.output,
                reference=item.reference,
                judges=[judge_provider],
                exclude=(),
                classification=classification,
            )
        return verdict.score, verdict.responses

    scored = await asyncio.gather(*(one(item) for item in items))
    judge_scores = [s for s, _ in scored]
    human_scores = [item.human_score for item in items]
    kappa = quadratic_weighted_kappa(judge_scores, human_scores)
    rho = spearman([float(s) for s in judge_scores], [float(h) for h in human_scores])
    agreement = sum(j == h for j, h in zip(judge_scores, human_scores, strict=True)) / len(items)
    bias = sum(j - h for j, h in zip(judge_scores, human_scores, strict=True)) / len(items)
    mapping = isotonic_fit([float(s) for s in judge_scores], [float(h) for h in human_scores])
    result = CalibrationResult(
        calibration_id=new_id(),
        judge_provider=judge_provider,
        rubric_digest=content_digest(rubric),
        items=len(items),
        weighted_kappa=round(kappa, 6),
        spearman=round(rho, 6),
        exact_agreement=round(agreement, 6),
        mean_bias=round(bias, 6),
        mapping=tuple((x, round(y, 6)) for x, y in mapping),
        calibrated=len(items) >= min_items and kappa >= min_kappa,
        created_by=created_by,
        created_at=now,
    )
    return CalibrationOutcome(
        result=result, responses=tuple(r for _, responses in scored for r in responses)
    )
