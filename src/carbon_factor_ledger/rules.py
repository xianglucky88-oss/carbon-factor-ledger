"""Versioned rule loading: factor registry, unit rules and evidence manifest.

All rule files are plain, local JSON documents so an audit server can rebuild
offline.  Every loader verifies an embedded version and, where applicable, a
content hash.  The returned objects are immutable dataclasses; their
``version``/``sha256`` values are the exact values later bound into the
footprint envelope and release manifest.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from .errors import EvidenceError, FactorError, RuleError, UnitError

# Canonical serialization settings.  Used for every content hash so that hashes
# are independent of input file ordering / on-disk whitespace.
CANONICAL_KW: Mapping[str, Any] = {
    "sort_keys": True,
    "separators": (",", ":"),
    "ensure_ascii": False,
}

RULE_FILENAMES = {
    "factors": "factors.json",
    "units": "unit_rules.json",
    "evidence": "evidence_manifest.json",
}


def canonical_json(obj: Any) -> bytes:
    """Serialize ``obj`` to canonical UTF-8 bytes used for hashing."""
    return json.dumps(obj, **CANONICAL_KW).encode("utf-8")


def order_insensitive(obj: Any) -> Any:
    """Return a copy of ``obj`` with every list content sorted canonically.

    Dicts keep their keys (hashing already sorts them), but lists are
    normalized so that input traversal/array order cannot change the
    order-insensitive identity hash.  Non-sortable mixed lists fall back to
    canonical JSON strings.
    """
    if isinstance(obj, dict):
        return {key: order_insensitive(value) for key, value in obj.items()}
    if isinstance(obj, list):
        normalized = [order_insensitive(item) for item in obj]
        try:
            normalized.sort(key=lambda item: json.dumps(item, **CANONICAL_KW))
        except TypeError:  # pragma: no cover - defensive mixed-type fallback
            normalized = sorted(
                normalized, key=lambda item: json.dumps(item, **CANONICAL_KW)
            )
        return normalized
    return obj


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _load_json(path: Path, label: str) -> dict:
    if not path.is_file():
        raise RuleError(f"{label} rule file not found: {path}")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise RuleError(f"{label} rule file is not valid JSON ({path}): {exc}") from exc
    if not isinstance(payload, dict):
        raise RuleError(f"{label} rule file must contain a JSON object: {path}")
    return payload


def _require_version(payload: dict, label: str) -> str:
    version = payload.get("version")
    if not isinstance(version, str) or not version:
        raise RuleError(f"{label} rule file is missing a non-empty 'version'")
    return version


@dataclass(frozen=True)
class Factor:
    factor_id: str
    value: float
    unit: str
    valid_from: str | None
    valid_to: str | None
    status: str
    source: str


@dataclass(frozen=True)
class FactorRegistry:
    version: str
    sha256: str
    factors: Mapping[str, Factor]

    def get(self, factor_id: str) -> Factor:
        factor = self.factors.get(factor_id)
        if factor is None:
            raise FactorError(f"unknown factor_id: {factor_id!r}")
        return factor


# Factor statuses that make a row ineligible before date evaluation.
_INACTIVE_STATUSES = frozenset({"invalid", "retired", "superseded"})
_ACTIVE_STATUSES = frozenset({"active"})
_KNOWN_STATUSES = _ACTIVE_STATUSES | _INACTIVE_STATUSES


def factor_state(factor: Factor, evaluation_date: str | None) -> tuple[bool, str]:
    """Return ``(usable, reason)`` for a factor at ``evaluation_date``.

    ``reason`` is a stable machine code: ``ok``, ``invalid``, ``retired``,
    ``expired``, ``not_yet_effective`` or ``no_evaluation_date``.
    Dates compare lexicographically because rule files use ISO ``YYYY-MM-DD``.
    """
    status = factor.status
    if status in _INACTIVE_STATUSES:
        return False, status
    if status not in _ACTIVE_STATUSES:
        return False, "invalid"
    if evaluation_date is None:
        return False, "no_evaluation_date"
    if factor.valid_from is not None and evaluation_date < factor.valid_from:
        return False, "not_yet_effective"
    if factor.valid_to is not None and evaluation_date > factor.valid_to:
        return False, "expired"
    return True, "ok"


def load_factor_registry(path: str | Path) -> FactorRegistry:
    path = Path(path)
    payload = _load_json(path, "factor")
    version = _require_version(payload, "factor")
    entries = payload.get("factors")
    if not isinstance(entries, list) or not entries:
        raise RuleError("factor registry must contain a non-empty 'factors' list")
    factors: dict[str, Factor] = {}
    for entry in entries:
        if not isinstance(entry, dict):
            raise RuleError("every factor entry must be a JSON object")
        factor_id = entry.get("factor_id")
        if not isinstance(factor_id, str) or not factor_id:
            raise RuleError("factor entry is missing a non-empty 'factor_id'")
        if factor_id in factors:
            raise RuleError(f"duplicate factor_id in registry: {factor_id}")
        try:
            value = float(entry["factor"])
        except (KeyError, TypeError, ValueError) as exc:
            raise RuleError(f"factor {factor_id!r} has an invalid numeric 'factor'") from exc
        unit = entry.get("unit")
        if not isinstance(unit, str) or not unit:
            raise RuleError(f"factor {factor_id!r} is missing 'unit'")
        status = entry.get("status", "active")
        if status not in _KNOWN_STATUSES:
            raise RuleError(f"factor {factor_id!r} has unknown status {status!r}")
        valid_from = entry.get("valid_from")
        valid_to = entry.get("valid_to")
        for name, date in (("valid_from", valid_from), ("valid_to", valid_to)):
            if date is not None and not isinstance(date, str):
                raise RuleError(f"factor {factor_id!r} field {name} must be a string")
        source = entry.get("source", "")
        if not isinstance(source, str):
            raise RuleError(f"factor {factor_id!r} 'source' must be a string")
        factors[factor_id] = Factor(
            factor_id=factor_id,
            value=value,
            unit=unit,
            valid_from=valid_from,
            valid_to=valid_to,
            status=status,
            source=source,
        )
    # Hash the normalized payload so formatting differences cannot change it.
    return FactorRegistry(
        version=version,
        sha256=sha256_bytes(canonical_json(payload)),
        factors=factors,
    )


@dataclass(frozen=True)
class UnitRules:
    version: str
    sha256: str
    base_unit: str
    factors: Mapping[str, float]

    def to_base(self, unit: str, quantity: float) -> tuple[float, float]:
        """Return ``(converted_quantity, multiplier)`` for ``unit``."""
        multiplier = self.factors.get(unit)
        if multiplier is None:
            raise UnitError(f"no conversion rule for unit {unit!r} (base={self.base_unit!r})")
        return float(quantity) * multiplier, multiplier


def load_unit_rules(path: str | Path) -> UnitRules:
    path = Path(path)
    payload = _load_json(path, "unit")
    version = _require_version(payload, "unit")
    base_unit = payload.get("base_unit")
    if not isinstance(base_unit, str) or not base_unit:
        raise RuleError("unit rules must declare a non-empty 'base_unit'")
    conversions = payload.get("conversions")
    if not isinstance(conversions, dict) or not conversions:
        raise RuleError("unit rules must contain a non-empty 'conversions' object")
    factors: dict[str, float] = {}
    for unit, multiplier in conversions.items():
        if not isinstance(unit, str) or not unit:
            raise RuleError("unit conversion keys must be non-empty strings")
        try:
            value = float(multiplier)
        except (TypeError, ValueError) as exc:
            raise RuleError(f"conversion for unit {unit!r} is not numeric") from exc
        if value <= 0:
            raise RuleError(f"conversion for unit {unit!r} must be positive, got {value}")
        factors[unit] = value
    factors.setdefault(base_unit, 1.0)
    return UnitRules(
        version=version,
        sha256=sha256_bytes(canonical_json(payload)),
        base_unit=base_unit,
        factors=factors,
    )


@dataclass(frozen=True)
class EvidenceFile:
    evidence_id: str
    path: str
    sha256: str
    description: str


@dataclass(frozen=True)
class EvidenceManifest:
    version: str
    sha256: str
    base_dir: Path
    files: Mapping[str, EvidenceFile]

    def verify(self, evidence_ids: list[str]) -> dict[str, str]:
        """Verify every referenced evidence id exists and its hash matches.

        Returns a deterministic ``evidence_id -> sha256`` map for the envelope.
        """
        resolved: dict[str, str] = {}
        for evidence_id in sorted(set(evidence_ids)):
            entry = self.files.get(evidence_id)
            if entry is None:
                raise EvidenceError(f"unknown evidence_id referenced by input: {evidence_id!r}")
            if not entry.path:
                raise EvidenceError(f"evidence {evidence_id!r} has an empty path")
            on_disk = self.base_dir / entry.path
            if not on_disk.is_file():
                raise EvidenceError(f"evidence file missing for {evidence_id!r}: {on_disk}")
            actual = sha256_file(on_disk)
            if actual != entry.sha256:
                raise EvidenceError(
                    f"evidence hash mismatch for {evidence_id!r}: "
                    f"manifest={entry.sha256} actual={actual}"
                )
            resolved[evidence_id] = actual
        return resolved


def load_evidence_manifest(path: str | Path) -> EvidenceManifest:
    path = Path(path)
    payload = _load_json(path, "evidence")
    version = _require_version(payload, "evidence")
    base_dir_value = payload.get("base_dir", ".")
    if not isinstance(base_dir_value, str) or not base_dir_value.strip():
        raise RuleError("evidence manifest 'base_dir' must be a non-empty string")
    entries = payload.get("files")
    if not isinstance(entries, list) or not entries:
        raise RuleError("evidence manifest must contain a non-empty 'files' list")
    files: dict[str, EvidenceFile] = {}
    for entry in entries:
        if not isinstance(entry, dict):
            raise RuleError("every evidence entry must be a JSON object")
        evidence_id = entry.get("evidence_id")
        if not isinstance(evidence_id, str) or not evidence_id:
            raise RuleError("evidence entry is missing a non-empty 'evidence_id'")
        if evidence_id in files:
            raise RuleError(f"duplicate evidence_id in manifest: {evidence_id}")
        rel_path = entry.get("path")
        # An explicitly empty path is an auditable error condition, not a
        # default that may silently resolve somewhere else.
        if not isinstance(rel_path, str) or not rel_path.strip():
            raise EvidenceError(f"evidence {evidence_id!r} has an empty path")
        if Path(rel_path).is_absolute() or ".." in Path(rel_path).parts:
            raise RuleError(
                f"evidence {evidence_id!r} path must be relative without '..': {rel_path}"
            )
        digest = entry.get("sha256")
        if not isinstance(digest, str) or len(digest) != 64:
            raise RuleError(f"evidence {evidence_id!r} is missing a 64-char sha256")
        description = entry.get("description", "")
        if not isinstance(description, str):
            raise RuleError(f"evidence {evidence_id!r} 'description' must be a string")
        files[evidence_id] = EvidenceFile(
            evidence_id=evidence_id,
            path=rel_path,
            sha256=digest.lower(),
            description=description,
        )
    base_dir = (path.parent / base_dir_value).resolve()
    return EvidenceManifest(
        version=version,
        sha256=sha256_bytes(canonical_json(payload)),
        base_dir=base_dir,
        files=files,
    )
