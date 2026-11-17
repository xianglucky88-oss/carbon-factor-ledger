"""Orchestration boundary: input + versioned rules -> bound result envelope.

The pipeline is the only component that knows where rule files live.  It loads
and verifies every rule set, runs the pure engine, verifies evidence hashes,
and binds the rule versions/hashes plus the input hash into the envelope so
each footprint result is self-describing for offline audit.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .engine import calculate
from .errors import EvidenceError, ValidationError
from .parser import parse_input
from .report import build_result
from .rules import (
    RULE_FILENAMES,
    EvidenceManifest,
    FactorRegistry,
    UnitRules,
    canonical_json,
    load_evidence_manifest,
    load_factor_registry,
    load_unit_rules,
    order_insensitive,
    sha256_bytes,
)

PROJECT_NAME = "carbon-factor-ledger"


def default_rules_dir() -> Path:
    """Return the packaged deterministic fixture rules directory."""
    return Path(__file__).resolve().parents[2] / "fixtures" / "rules"


def load_rule_bundle(
    rules_dir: str | Path | None = None,
) -> tuple[FactorRegistry, UnitRules, EvidenceManifest]:
    rules_dir = Path(rules_dir) if rules_dir is not None else default_rules_dir()
    factors = load_factor_registry(rules_dir / RULE_FILENAMES["factors"])
    units = load_unit_rules(rules_dir / RULE_FILENAMES["units"])
    evidence = load_evidence_manifest(rules_dir / RULE_FILENAMES["evidence"])
    return factors, units, evidence


def run(payload: dict[str, Any], rules_dir: str | Path | None = None) -> dict[str, Any]:
    """Execute the full pipeline against an in-memory input payload."""
    if not isinstance(payload, dict):
        raise ValidationError("input payload must be a JSON object")
    factors, units, evidence = load_rule_bundle(rules_dir)
    rows, evaluation_date = parse_input(payload)

    data = calculate(
        rows,
        payload,
        factors=factors,
        units=units,
        evaluation_date=evaluation_date,
    )
    # Verify the actual evidence files on disk against the pinned hashes.
    verified = evidence.verify(data["referenced_evidence"])
    if data["referenced_evidence"] and not verified:
        raise EvidenceError("evidence verification produced an empty binding")

    envelope = build_result(PROJECT_NAME, data)
    # Three input identities are bound:
    #   sha256_raw         - exact submitted bytes (tamper evidence);
    #   sha256_canonical   - whitespace/key-order-insensitive serialization;
    #   sha256_normalized  - also insensitive to activities array order.
    envelope["input"] = {
        "sha256_raw": sha256_bytes(
            json.dumps(payload, ensure_ascii=False).encode("utf-8")
        ),
        "sha256_canonical": sha256_bytes(canonical_json(payload)),
        "sha256_normalized": sha256_bytes(
            canonical_json(order_insensitive(payload))
        ),
        "evaluation_date": evaluation_date,
    }
    envelope["rule_binding"] = {
        "factors": {"version": factors.version, "sha256": factors.sha256},
        "units": {"version": units.version, "sha256": units.sha256},
        "evidence": {"version": evidence.version, "sha256": evidence.sha256},
    }
    envelope["evidence_files"] = [
        {
            "evidence_id": evidence_id,
            "path": evidence.files[evidence_id].path,
            "sha256": digest,
        }
        for evidence_id, digest in sorted(verified.items())
    ]
    return envelope


def run_file(input_path: str | Path, rules_dir: str | Path | None = None) -> dict[str, Any]:
    """Read an input JSON file from disk and run the pipeline."""
    path = Path(input_path)
    payload = json.loads(path.read_text(encoding="utf-8"))
    return run(payload, rules_dir)
