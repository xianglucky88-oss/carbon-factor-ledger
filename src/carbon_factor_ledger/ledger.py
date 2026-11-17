"""The event-sourced factor ledger (state + write model).

The ledger is the *only* mutable stateful component.  All writes go through
validated events; each applied event appends to a hash-chained event log whose
state hash uniquely identifies the ledger contents.  Recomputation strategies
never mutate this object directly.

Design properties
-----------------
* Content-addressed events make identical imports idempotent.
* ``apply_batch`` is transactional: a failure restores the pre-batch snapshot,
  so partial application cannot leave temporary state behind.
* Factor versions and evidence are immutable; replacement means *new version*,
  withdrawal is a status flag on a version, never an in-place edit.
* Frozen reports are stored copies, so later events structurally cannot
  overwrite them; they can only be marked ``superseded``.
"""
from __future__ import annotations

import copy
import hashlib
from dataclasses import replace
from datetime import date, datetime
from typing import Any, Dict, List, Optional, Set, Tuple

from .models import (
    BatchResult,
    ConsumptionRecord,
    Event,
    Evidence,
    FactorVersion,
    FrozenReport,
)
from .parser import canonical_json
from .units import UnitError, parse_factor_unit


class LedgerError(ValueError):
    """A referential or business-rule violation while applying an event."""


class Ledger:
    def __init__(self) -> None:
        self.events: List[Event] = []
        self._event_ids: Set[str] = set()
        self.evidence: Dict[str, Evidence] = {}
        # evidence_id -> {"sha256": original hash, "conflicts": set of hashes,
        #                  "imports": number of registration events}
        self._evidence_state: Dict[str, Dict[str, Any]] = {}
        self.factors: Dict[Tuple[str, int], FactorVersion] = {}
        self.records: Dict[str, ConsumptionRecord] = {}
        self._cells: Dict[Tuple[str, str], List[str]] = {}
        self.reports: Dict[str, FrozenReport] = {}
        self._state_hash = hashlib.sha256(b"empty-ledger").hexdigest()

    # ------------------------------------------------------------------ clone
    def clone(self) -> "Ledger":
        """Deep copy used for transactional batches and failure recovery."""
        return copy.deepcopy(self)

    @property
    def state_hash(self) -> str:
        return self._state_hash

    # ------------------------------------------------------------- event log
    def _append_event(self, event: Event) -> None:
        self.events.append(event)
        self._event_ids.add(event.event_id)
        chain = hashlib.sha256()
        chain.update(self._state_hash.encode())
        chain.update(event.event_id.encode())
        self._state_hash = chain.hexdigest()

    def has_event(self, event_id: str) -> bool:
        return event_id in self._event_ids

    # -------------------------------------------------------------- evidence
    def evidence_conflicts(self) -> Dict[str, Dict[str, Any]]:
        return {
            eid: state
            for eid, state in self._evidence_state.items()
            if state["conflicts"]
        }

    def _apply_evidence(self, event: Event) -> None:
        p = event.payload
        eid = p["evidence_id"]
        state = self._evidence_state.get(eid)
        if state is None:
            self._evidence_state[eid] = {
                "sha256": p["sha256"],
                "conflicts": set(),
                "imports": 1,
            }
            self.evidence[eid] = Evidence(
                evidence_id=eid,
                filename=p["filename"],
                sha256=p["sha256"],
                registered_at=event.timestamp,
            )
            return
        state["imports"] += 1
        if p["sha256"] != state["sha256"]:
            # Same evidence identity, different file content: the chain is
            # broken.  Keep the original hash as the attested one and record
            # the collision; applicability will quarantine dependent records.
            state["conflicts"].add(p["sha256"])

    # -------------------------------------------------------------- factors
    def _apply_factor_version(self, event: Event) -> None:
        p = event.payload
        key = (p["factor_id"], p["version"])
        if key in self.factors:
            existing = self.factors[key]
            if self._factor_signature(existing) == self._payload_factor_signature(p):
                return  # identical re-import: idempotent no-op
            raise LedgerError(
                f"factor {p['factor_id']} v{p['version']} already exists "
                "with different content; register a new version instead"
            )
        numerator, reference_unit = parse_factor_unit(p["factor_unit"])
        valid_to = parse_date_or_none(p.get("valid_to"))
        self.factors[key] = FactorVersion(
            factor_id=p["factor_id"],
            version=p["version"],
            activity=p["activity"],
            material=p["material"],
            factor_value=float(p["factor_value"]),
            factor_unit_numerator=numerator,
            reference_unit=reference_unit,
            geography=p["geography"],
            boundary=p["boundary"],
            valid_from=date.fromisoformat(p["valid_from"]),
            valid_to=valid_to,
            evidence_id=p["evidence_id"],
            supersedes=p.get("supersedes"),
            registered_at=event.timestamp,
        )

    @staticmethod
    def _payload_factor_signature(p: Dict[str, Any]) -> str:
        keys = (
            "activity", "material", "factor_value", "factor_unit", "geography",
            "boundary", "valid_from", "valid_to", "evidence_id",
        )
        return canonical_json({k: p.get(k) for k in keys})

    def _factor_signature(self, f: FactorVersion) -> str:
        return canonical_json({
            "activity": f.activity,
            "material": f.material,
            "factor_value": f.factor_value,
            "factor_unit": f"{f.factor_unit_numerator}/{f.reference_unit}",
            "geography": f.geography,
            "boundary": f.boundary,
            "valid_from": f.valid_from.isoformat(),
            "valid_to": f.valid_to.isoformat() if f.valid_to else None,
            "evidence_id": f.evidence_id,
        })

    def _apply_withdrawal(self, event: Event) -> None:
        p = event.payload
        key = (p["factor_id"], p["version"])
        factor = self.factors.get(key)
        if factor is None:
            raise LedgerError(
                f"cannot withdraw unknown factor {p['factor_id']} v{p['version']}"
            )
        if factor.status == "withdrawn":
            return  # idempotent re-import of the same withdrawal
        self.factors[key] = replace(factor, status="withdrawn", withdrawn_at=event.timestamp)

    # -------------------------------------------------------------- records
    def _apply_consumption(self, event: Event) -> None:
        p = event.payload
        rid = p["record_id"]
        occurred = date.fromisoformat(parse_date_str(p["occurred_on"]))
        period = p.get("period") or f"{occurred.year}-Q{((occurred.month - 1) // 3) + 1}"
        record = ConsumptionRecord(
            record_id=rid,
            product=p["product"],
            material=p["material"],
            activity=p["activity"],
            quantity=float(p["quantity"]),
            unit=p["unit"],
            geography=p["geography"],
            boundary=p["boundary"],
            occurred_on=occurred,
            period=period,
            factor_id=p.get("factor_id"),
            factor_version=p.get("factor_version"),
            event_id=event.event_id,
        )
        if rid in self.records:
            if self.records[rid] == record:
                return  # identical re-import: idempotent no-op
            raise LedgerError(
                f"record {rid!r} already exists with different content; "
                "use a new record_id"
            )
        self.records[rid] = record
        self._cells.setdefault((record.product, record.period), []).append(rid)

    # --------------------------------------------------------------- reports
    def _apply_freeze(self, event: Event) -> None:
        # Imported locally to keep the ledger -> engine dependency one-way at
        # module load time (engine only needs the ledger type at runtime).
        from .engine import evaluate_cell

        p = event.payload
        report_id = p["report_id"]
        if report_id in self.reports:
            raise LedgerError(f"report {report_id!r} is already frozen; freeze a new id")
        evaluation = evaluate_cell(self, p["product"], p["period"])
        footprint = evaluation.to_footprint()
        self.reports[report_id] = FrozenReport(
            report_id=report_id,
            product=p["product"],
            period=p["period"],
            frozen_at=event.timestamp,
            ledger_state_hash=self._state_hash,
            footprint=footprint.footprint,
            included_count=footprint.included_count,
            quarantined_count=footprint.quarantined_count,
            contributions=footprint.contributions,
            quarantined=footprint.quarantined,
        )

    def mark_report_superseded(
        self, report_id: str, at: datetime, causing_event_ids: Tuple[str, ...]
    ) -> None:
        report = self.reports[report_id]
        if report.status == "superseded":
            merged = tuple(sorted(set(report.superseded_event_ids) | set(causing_event_ids)))
            self.reports[report_id] = replace(report, superseded_event_ids=merged)
            return
        self.reports[report_id] = replace(
            report,
            status="superseded",
            superseded_at=at,
            superseded_event_ids=tuple(sorted(causing_event_ids)),
        )

    # --------------------------------------------------------------- dispatch
    _DISPATCH = {
        "evidence_registered": "_apply_evidence",
        "factor_version_registered": "_apply_factor_version",
        "factor_withdrawn": "_apply_withdrawal",
        "consumption_recorded": "_apply_consumption",
        "report_frozen": "_apply_freeze",
    }

    def apply_event(self, event: Event) -> None:
        if event.event_id in self._event_ids:
            return  # idempotent: same content hash already applied
        getattr(self, self._DISPATCH[event.event_type])(event)
        self._append_event(event)

    def apply_batch(self, events: List[Event]) -> BatchResult:
        """Apply events transactionally.

        A snapshot is taken first; on any error the ledger is rolled back to
        it and the offending event is reported, so failure never leaves
        half-applied temporary state.
        """
        snapshot = copy.deepcopy(self.__dict__)
        accepted = 0
        for event in events:
            try:
                self.apply_event(event)
                accepted += 1
            except (LedgerError, UnitError) as exc:
                # Roll back every mutation of this batch (including events
                # accepted earlier in it): no half-applied temporary state.
                self.__dict__ = copy.deepcopy(snapshot)
                return BatchResult(
                    accepted=accepted,
                    rejected=((event.event_id, str(exc)),),
                    state_hash=self._state_hash,
                )
        return BatchResult(accepted=accepted, rejected=(), state_hash=self._state_hash)

    # ---------------------------------------------------------------- queries
    def get_factor(self, factor_id: str, version: int) -> Optional[FactorVersion]:
        return self.factors.get((factor_id, version))

    def latest_version(self, factor_id: str) -> Optional[int]:
        versions = [v for (fid, v) in self.factors if fid == factor_id]
        return max(versions) if versions else None

    def versions_of(self, factor_id: str) -> List[FactorVersion]:
        return [
            self.factors[key]
            for key in sorted(self.factors)
            if key[0] == factor_id
        ]

    def resolve_factor(
        self, factor_id: str, pinned_version: Optional[int], on_date: date
    ) -> Optional[FactorVersion]:
        """Resolve the factor version applicable to a record date.

        Pinned version => that exact version.  Unpinned => highest-numbered
        version whose validity window covers ``on_date`` (valid_from inclusive,
        valid_to inclusive; open-ended when valid_to is None).
        """
        if pinned_version is not None:
            return self.get_factor(factor_id, pinned_version)
        candidates = [
            f for (fid, _v), f in self.factors.items()
            if fid == factor_id
            and f.valid_from <= on_date
            and (f.valid_to is None or on_date <= f.valid_to)
        ]
        if not candidates:
            return None
        # Withdrawn versions remain resolvable: withdrawal is an applicability
        # rule (FACTOR_WITHDRAWN), not invisibility. Prefer an active version
        # when both cover the date; otherwise return the withdrawn one so its
        # status and evidence chain can be judged explicitly.
        active = [f for f in candidates if f.status == "active"]
        pool = active or candidates
        return max(pool, key=lambda f: f.version)

    def cell_records(self, product: str, period: str) -> List[ConsumptionRecord]:
        return [self.records[rid] for rid in self._cells.get((product, period), [])]

    def all_cells(self) -> List[Tuple[str, str]]:
        return sorted(self._cells)

    def reports_for(self, product: str, period: str) -> List[FrozenReport]:
        return [
            r for r in self.reports.values()
            if r.product == product and r.period == period
        ]


def parse_date_str(value: Any) -> str:
    if isinstance(value, date):
        return value.isoformat()
    return str(value)


def parse_date_or_none(value: Any) -> Optional[date]:
    if value in (None, ""):
        return None
    return date.fromisoformat(parse_date_str(value)[:10])
