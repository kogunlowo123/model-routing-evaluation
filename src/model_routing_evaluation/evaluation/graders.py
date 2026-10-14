"""Deterministic graders: exact match, regular expressions, JSON Schema."""

from __future__ import annotations

import json
import re
from typing import Any

from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError

from model_routing_evaluation.domain.errors import UnprocessableError
from model_routing_evaluation.domain.evaluation import ExactGrader, Grade, RegexGrader

MAX_GRADED_CHARS = 64_000
MAX_CALLER_SCHEMA_BYTES = 16_384
MAX_CALLER_SCHEMA_DEPTH = 12
# Keywords that would let a caller run arbitrary regular expressions (ReDoS)
# or make the validator resolve remote documents.
FORBIDDEN_CALLER_KEYWORDS = frozenset(
    {"pattern", "patternProperties", "$ref", "$dynamicRef", "$recursiveRef", "format"}
)

_FENCE = re.compile(r"```(?:json)?\s*(.*?)```", re.DOTALL | re.IGNORECASE)


def _normalize(text: str, *, case_sensitive: bool, strip: bool) -> str:
    value = " ".join(text.split()) if strip else text
    return value if case_sensitive else value.casefold()


def grade_exact(spec: ExactGrader, expected: object, output: str) -> Grade:
    if not isinstance(expected, str):
        raise UnprocessableError("exact grader needs a string expected value")
    got = _normalize(output, case_sensitive=spec.case_sensitive, strip=spec.strip)
    want = _normalize(expected, case_sensitive=spec.case_sensitive, strip=spec.strip)
    passed = got == want
    return Grade(score=1.0 if passed else 0.0, passed=passed, detail="" if passed else "mismatch")


def grade_regex(spec: RegexGrader, output: str) -> Grade:
    text = output[:MAX_GRADED_CHARS]
    flags = re.IGNORECASE if spec.ignore_case else 0
    missed: list[str] = []
    for pattern in spec.patterns:
        compiled = re.compile(pattern, flags)
        matched = compiled.fullmatch(text) if spec.mode == "fullmatch" else compiled.search(text)
        if matched is None:
            missed.append(pattern)
    score = 1.0 - len(missed) / len(spec.patterns)
    return Grade(
        score=score,
        passed=not missed,
        detail="" if not missed else f"unmatched: {', '.join(missed)}",
    )


def extract_json(text: str) -> Any:
    """Parse JSON from a model answer: bare JSON, a fenced block, or the outermost object."""
    candidates = [text.strip()]
    candidates += [m.group(1).strip() for m in _FENCE.finditer(text)]
    start, end = text.find("{"), text.rfind("}")
    if 0 <= start < end:
        candidates.append(text[start : end + 1])
    for candidate in candidates:
        try:
            return json.loads(candidate)
        except ValueError:
            continue
    raise ValueError("no JSON document found in output")


def _same(expected: Any, actual: Any) -> bool:
    if isinstance(expected, str) and isinstance(actual, str):
        return " ".join(expected.split()).casefold() == " ".join(actual.split()).casefold()
    return bool(expected == actual)


def grade_json_schema(schema: dict[str, Any], expected: object, output: str) -> Grade:
    try:
        document = extract_json(output[:MAX_GRADED_CHARS])
    except ValueError:
        return Grade(score=0.0, passed=False, detail="output is not JSON")
    validator = Draft202012Validator(schema)
    error = next(iter(validator.iter_errors(document)), None)
    if error is not None:
        return Grade(score=0.0, passed=False, detail=f"schema violation: {error.message[:200]}")
    if not isinstance(expected, dict) or not expected:
        return Grade(score=1.0, passed=True)
    if not isinstance(document, dict):
        return Grade(score=0.0, passed=False, detail="expected a JSON object")
    wrong = sorted(k for k, v in expected.items() if not _same(v, document.get(k)))
    score = 1.0 - len(wrong) / len(expected)
    return Grade(
        score=score,
        passed=not wrong,
        detail="" if not wrong else f"fields differ: {', '.join(wrong)}",
    )


def _depth(value: Any, level: int = 0) -> int:
    if isinstance(value, dict):
        return max([level] + [_depth(v, level + 1) for v in value.values()])
    if isinstance(value, list):
        return max([level] + [_depth(v, level + 1) for v in value])
    return level


# Keywords whose object values map names (not keywords) to subschemas, and
# keywords whose values are instance data rather than schemas.
_NAMED_SUBSCHEMAS = frozenset({"properties", "$defs", "definitions", "dependentSchemas"})
_DATA_VALUES = frozenset({"enum", "const", "examples", "default", "required"})


def _keywords(node: Any) -> set[str]:
    found: set[str] = set()
    if isinstance(node, dict):
        for key, value in node.items():
            found.add(key)
            if key in _DATA_VALUES:
                continue
            if key in _NAMED_SUBSCHEMAS and isinstance(value, dict):
                for child in value.values():
                    found |= _keywords(child)
            else:
                found |= _keywords(value)
    elif isinstance(node, list):
        for item in node:
            found |= _keywords(item)
    return found


def check_caller_schema(schema: dict[str, Any]) -> None:
    """Validate a response schema supplied by an untrusted caller.

    Size and depth are bounded, and keywords that execute caller-supplied
    regular expressions or reference other documents are refused.
    """
    if len(json.dumps(schema)) > MAX_CALLER_SCHEMA_BYTES:
        raise UnprocessableError("response_schema is larger than 16 KiB")
    if _depth(schema) > MAX_CALLER_SCHEMA_DEPTH:
        raise UnprocessableError("response_schema is nested too deeply")
    forbidden = sorted(_keywords(schema) & FORBIDDEN_CALLER_KEYWORDS)
    if forbidden:
        raise UnprocessableError(f"response_schema may not use {forbidden}")
    try:
        Draft202012Validator.check_schema(schema)
    except SchemaError as exc:
        raise UnprocessableError(
            f"response_schema is not a valid JSON Schema: {exc.message}"
        ) from exc
