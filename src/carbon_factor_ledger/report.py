"""Stable result envelope.

The envelope is the sole boundary between domain data and serialized output.
It contains no wall-clock time, no hostname and no environment-dependent
field, so two runs over identical inputs produce identical bytes.
"""
from __future__ import annotations

from typing import Any

from .models import PipelineResult


def build_result(project: str, data: dict[str, Any]) -> dict[str, Any]:
    """Wrap domain ``data`` in the auditable result envelope."""
    result = PipelineResult(project=project, data=data)
    return {"project": result.project, "status": "ok", **result.data}
