"""Input validation and record extraction.

The parser performs structural validation only.  All domain decisions
(factor validity, unit conversion, evidence hashes) stay in the engine/rules
modules so this layer remains trivial to audit in isolation.
"""
from __future__ import annotations

from typing import Any

from .errors import ValidationError

_REQUIRED_ROW_FIELDS = ("product", "quantity", "unit", "factor_id", "evidence_id")
_NUMERIC_FIELDS = ("quantity",)


def _validate_evaluation_date(payload: dict) -> str:
    evaluation_date = payload.get("evaluation_date")
    if not isinstance(evaluation_date, str) or not evaluation_date.strip():
        raise ValidationError("'evaluation_date' is required (YYYY-MM-DD)")
    parts = evaluation_date.split("-")
    if len(parts) != 3 or not all(part.isdigit() for part in parts):
        raise ValidationError("'evaluation_date' must use the YYYY-MM-DD format")
    return evaluation_date


def _validate_row(row: Any, index: int) -> dict:
    if not isinstance(row, dict):
        raise ValidationError(f"activity[{index}] must be a JSON object")
    for field in _REQUIRED_ROW_FIELDS:
        if field not in row:
            raise ValidationError(f"activity[{index}] is missing required field {field!r}")
    for field in _REQUIRED_ROW_FIELDS:
        value = row[field]
        if field == "quantity":
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise ValidationError(f"activity[{index}].quantity must be numeric")
            if float(value) < 0:
                raise ValidationError(f"activity[{index}].quantity must not be negative")
        elif not isinstance(value, str) or not value.strip():
            raise ValidationError(f"activity[{index}].{field} must be a non-empty string")
    # Empty evidence path at the input level: fail loudly rather than silently
    # producing a footprint whose provenance cannot be reviewed.
    if not row["evidence_id"].strip():
        raise ValidationError(f"activity[{index}] has an empty evidence_id path")
    return dict(row)


def load_records(payload: dict) -> list[dict]:
    """Validate ``payload`` and return a fresh list of normalized row dicts."""
    if not isinstance(payload, dict):
        raise ValidationError("input payload must be a JSON object")
    rows = payload.get("activities", [])
    if not isinstance(rows, list):
        raise ValidationError("'activities' must be a list")
    if not rows:
        raise ValidationError("'activities' must contain at least one activity")
    return [_validate_row(row, index) for index, row in enumerate(rows)]


def parse_input(payload: dict) -> tuple[list[dict], str]:
    """Return validated rows plus the mandatory evaluation date."""
    return load_records(payload), _validate_evaluation_date(payload)
