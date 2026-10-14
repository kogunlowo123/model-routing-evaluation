"""Golden-set loading. Sets are YAML files reviewed and versioned with the code."""

from __future__ import annotations

from pathlib import Path

import yaml

from model_routing_evaluation.domain.evaluation import GoldenSet


def load_golden_sets(directory: Path) -> dict[str, GoldenSet]:
    sets: dict[str, GoldenSet] = {}
    for path in sorted(directory.glob("*.yaml")):
        golden = GoldenSet.model_validate(yaml.safe_load(path.read_text(encoding="utf-8")))
        if golden.name in sets:
            raise ValueError(f"duplicate golden set name {golden.name} in {path.name}")
        sets[golden.name] = golden
    return sets
