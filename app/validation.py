"""Parse a planner JSON string and validate it as a ProposedPlan.

A result is either a complete plan and no errors, or structured errors and no
plan. JSON parse failures use code ``json_invalid``. Schema failures use the
Pydantic error type as ``code``. Input is not repaired, coerced, or partially
accepted. This module does not apply policy, pricing, hashing, or persistence.
"""

import json
from collections.abc import Mapping
from dataclasses import dataclass

from pydantic import ValidationError

from app.models import ProposedPlan, ValidationIssue

_VALUE_ERROR_PREFIX = "Value error, "
_INTERPRETATION_FIELDS = frozenset({"region", "resources"})


@dataclass(frozen=True)
class ValidationResult:
    proposed: ProposedPlan | None
    errors: tuple[ValidationIssue, ...]

    def __post_init__(self) -> None:
        has_plan = self.proposed is not None
        has_errors = len(self.errors) > 0
        if has_plan == has_errors:
            raise ValueError("validation must return a plan with no errors, or errors with no plan")


def validate_raw_plan(raw: str) -> ValidationResult:
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        return _failure(_json_issue(exc))
    interpretation = _interpretation_issue(payload)
    if interpretation is not None:
        return _failure(interpretation)
    try:
        proposed = ProposedPlan.model_validate(payload)
    except ValidationError as exc:
        return _failure(*(_schema_issue(error) for error in exc.errors()))
    return ValidationResult(proposed=proposed, errors=())


def _interpretation_issue(payload: object) -> ValidationIssue | None:
    if not isinstance(payload, dict):
        return None
    message = payload.get("interpretation_error")
    if not isinstance(message, str) or message.strip() == "":
        return None
    field = payload.get("interpretation_field")
    field_path = field if field in _INTERPRETATION_FIELDS else "region"
    return ValidationIssue(code="unrecognized_input", message=message, field_path=field_path)


def _failure(*issues: ValidationIssue) -> ValidationResult:
    return ValidationResult(proposed=None, errors=issues)


def _json_issue(exc: json.JSONDecodeError) -> ValidationIssue:
    return ValidationIssue(
        code="json_invalid",
        message=f"{exc.msg} at line {exc.lineno} column {exc.colno}",
    )


def _schema_issue(error: Mapping[str, object]) -> ValidationIssue:
    message = str(error["msg"])
    if message.startswith(_VALUE_ERROR_PREFIX):
        message = message[len(_VALUE_ERROR_PREFIX) :]
    return ValidationIssue(
        code=str(error["type"]),
        message=message,
        field_path=_field_path(error["loc"]),
    )


def _field_path(loc: object) -> str | None:
    if not isinstance(loc, tuple) or not loc:
        return None
    parts: list[str] = []
    for item in loc:
        if isinstance(item, int):
            if parts:
                parts[-1] = f"{parts[-1]}[{item}]"
            else:
                parts.append(f"[{item}]")
        else:
            parts.append(str(item))
    return ".".join(parts)
