"""Immutable domain contracts for the carbon factor ledger.

Every object that crosses a module boundary is a frozen dataclass so that
historical state (imported events, frozen reports, computed footprints) cannot
be mutated in place.  Recomputation always produces *new* objects; frozen
reports are therefore structurally impossible to overwrite.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any, Dict, List, Optional, Tuple

# ---------------------------------------------------------------------------
# Canonical vocabulary (unified materials, activities, units, geography).
# ---------------------------------------------------------------------------

#: Reasons a consumption record is isolated from the paid footprint.  A record
#: never silently counts as zero: it either contributes, or appears here.
QUARANTINE_REASONS = (
    "FACTOR_NOT_FOUND",
    "EVIDENCE_MISSING",
    "EVIDENCE_HASH_MISMATCH",
    "FACTOR_WITHDRAWN",
    "FACTOR_EXPIRED",
    "FACTOR_NOT_YET_VALID",
    "GEOGRAPHY_OUT_OF_SCOPE",
    "BOUNDARY_MISMATCH",
    "UNIT_INCOMPATIBLE",
)

#: Event types accepted by the ledger, in canonical spelling.
EVENT_TYPES = (
    "evidence_registered",
    "factor_version_registered",
    "factor_withdrawn",
    "consumption_recorded",
    "report_frozen",
)

# ---------------------------------------------------------------------------
# Input events (the only write path into the ledger).
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Event:
    """A single, content-addressed ledger event.

    ``event_id`` is derived from the canonical JSON of the event payload when
    the caller does not supply one, which makes identical imports idempotent.
    """

    seq: int
    event_id: str
    event_type: str
    timestamp: datetime
    payload: Dict[str, Any]


@dataclass(frozen=True)
class Evidence:
    evidence_id: str
    filename: str
    sha256: str
    registered_at: datetime


@dataclass(frozen=True)
class FactorVersion:
    """One immutable version of an emission factor.

    A factor is a versioned entity (``factor_id`` + ``version``).  Replacement
    is expressed by registering a newer version whose validity window starts
    later; withdrawal is a separate event and never mutates a version row.
    """

    factor_id: str
    version: int
    activity: str
    material: str
    factor_value: float
    factor_unit_numerator: str          # emission unit, e.g. kgCO2e / tCO2e
    reference_unit: str                 # activity unit, e.g. kg / kWh / tkm
    geography: str
    boundary: str                       # lifecycle boundary, or "ALL"
    valid_from: date
    valid_to: Optional[date]
    evidence_id: str
    supersedes: Optional[int]
    registered_at: datetime
    status: str = "active"              # active | withdrawn
    withdrawn_at: Optional[datetime] = None


@dataclass(frozen=True)
class ConsumptionRecord:
    record_id: str
    product: str
    material: str
    activity: str
    quantity: float
    unit: str
    geography: str
    boundary: str
    occurred_on: date
    period: str
    factor_id: Optional[str]
    factor_version: Optional[int]       # None => resolve latest applicable
    event_id: str


@dataclass(frozen=True)
class FrozenReport:
    """An immutable report snapshot.

    Freezing copies the *values* computed at freeze time together with the hash
    of the ledger state they were computed from.  Later factor replacement or
    withdrawal can mark a report ``superseded`` for downstream objects but can
    never change its stored footprint.
    """

    report_id: str
    product: str
    period: str
    frozen_at: datetime
    ledger_state_hash: str
    footprint: float
    included_count: int
    quarantined_count: int
    status: str = "frozen"              # frozen | superseded
    superseded_at: Optional[datetime] = None
    superseded_event_ids: Tuple[str, ...] = field(default_factory=tuple)
    contributions: Tuple["Contribution", ...] = field(default_factory=tuple)
    quarantined: Tuple["QuarantineEntry", ...] = field(default_factory=tuple)


# ---------------------------------------------------------------------------
# Computed results.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Contribution:
    record_id: str
    factor_id: str
    factor_version: int
    material: str
    activity: str
    quantity: float
    unit: str
    converted_quantity: float
    reference_unit: str
    emission: float                     # kgCO2e
    evidence_id: str
    evidence_sha256: str


@dataclass(frozen=True)
class QuarantineEntry:
    record_id: str
    product: str
    period: str
    factor_id: Optional[str]
    factor_version: Optional[int]
    reasons: Tuple[str, ...]
    detail: str
    quantity: float
    unit: str


@dataclass(frozen=True)
class ProductFootprint:
    product: str
    period: str
    footprint: float                    # kgCO2e, included records only
    included_count: int
    quarantined_count: int
    status: str                         # COMPLETE | INCOMPLETE
    coverage: float                     # included quantity / total quantity
    contributions: Tuple[Contribution, ...]
    quarantined: Tuple[QuarantineEntry, ...]
    evidence_refs: Tuple[str, ...]      # de-duplicated evidence identifiers


@dataclass(frozen=True)
class Evaluation:
    """Result of evaluating one (product, period) cell against the ledger."""

    product: str
    period: str
    contributions: Tuple[Contribution, ...]
    quarantined: Tuple[QuarantineEntry, ...]

    def to_footprint(self) -> ProductFootprint:
        total_records = len(self.contributions) + len(self.quarantined)
        # Coverage is record-based on purpose: cells can mix quantities in
        # incompatible dimensions (kg, kWh, tkm), so summing quantities is not
        # a meaningful denominator.
        coverage = (
            len(self.contributions) / total_records if total_records else 1.0
        )
        return ProductFootprint(
            product=self.product,
            period=self.period,
            footprint=round(sum(c.emission for c in self.contributions), 6),
            included_count=len(self.contributions),
            quarantined_count=len(self.quarantined),
            status="INCOMPLETE" if self.quarantined else "COMPLETE",
            coverage=round(coverage, 6),
            contributions=tuple(sorted(self.contributions, key=lambda c: c.record_id)),
            quarantined=tuple(sorted(self.quarantined, key=lambda q: q.record_id)),
            evidence_refs=tuple(
                sorted({c.evidence_id for c in self.contributions})
            ),
        )


@dataclass(frozen=True)
class RecomputeStats:
    """Observability counters used to bound recomputation scope/temp state."""

    strategy: str
    events_applied: int
    cells_evaluated: int
    records_touched: int
    factor_versions_touched: int
    affected_products: Tuple[str, ...]
    affected_periods: Tuple[str, ...]
    affected_reports: Tuple[str, ...]


@dataclass(frozen=True)
class BatchResult:
    accepted: int
    rejected: Tuple[Tuple[str, str], ...]      # (event_id_or_index, error)
    state_hash: str
