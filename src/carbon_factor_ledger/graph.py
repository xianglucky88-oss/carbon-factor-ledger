"""Strategy B — factor dependency graph with incremental propagation.

Nodes
-----
* evidence nodes (attested files),
* factor version nodes (factor_id groups versions; a record binds a factor_id
  and the ledger resolves the applicable version at evaluation time),
* consumption record nodes,
* (product, period) cells as aggregation sinks.

Edges encode dependencies: ``record -> factor_id -> evidence``.  A change event
marks source nodes and propagates *against* the edge direction
(evidence -> factors -> records -> cells), so only records that can actually
have changed are re-evaluated.  Per-cell aggregates are then adjusted by the
exact emission delta of each touched record — no full-cell or full-ledger
rebuild, and no temporary full-state copy.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Set, Tuple

from .applicability import evaluate_record
from .engine import mark_affected_reports
from .ledger import Ledger
from .models import (
    Contribution,
    Event,
    ProductFootprint,
    QuarantineEntry,
    RecomputeStats,
)


@dataclass
class _CellState:
    record_ids: List[str] = field(default_factory=list)
    contributions: Dict[str, Contribution] = field(default_factory=dict)
    quarantined: Dict[str, QuarantineEntry] = field(default_factory=dict)

    def to_footprint(self, product: str, period: str) -> ProductFootprint:
        contributions = tuple(
            sorted(self.contributions.values(), key=lambda c: c.record_id)
        )
        quarantined = tuple(
            sorted(self.quarantined.values(), key=lambda q: q.record_id)
        )
        total_records = len(contributions) + len(quarantined)
        return ProductFootprint(
            product=product,
            period=period,
            footprint=round(sum(c.emission for c in contributions), 6),
            included_count=len(contributions),
            quarantined_count=len(quarantined),
            status="INCOMPLETE" if quarantined else "COMPLETE",
            coverage=round(len(contributions) / total_records, 6)
            if total_records else 1.0,
            contributions=contributions,
            quarantined=quarantined,
            evidence_refs=tuple(sorted({c.evidence_id for c in contributions})),
        )


class DependencyGraph:
    def __init__(self, ledger: Ledger) -> None:
        self.ledger = ledger
        # record -> factor_id it references (edge record -> factor)
        self.record_factor: Dict[str, Optional[str]] = {}
        # factor_id -> records that reference it (reverse edge)
        self.factor_records: Dict[str, Set[str]] = {}
        # evidence_id -> factor_ids attesting it (reverse edge factor -> evidence)
        self.evidence_factors: Dict[str, Set[str]] = {}
        self.cells: Dict[Tuple[str, str], _CellState] = {}
        self._record_result: Dict[str, Any] = {}

    # ------------------------------------------------------------- building
    def load_full(self) -> None:
        self.record_factor.clear()
        self.factor_records.clear()
        self.evidence_factors.clear()
        self.cells.clear()
        self._record_result.clear()
        for factor in self.ledger.factors.values():
            self.evidence_factors.setdefault(factor.evidence_id, set()).add(
                factor.factor_id
            )
        for record in self.ledger.records.values():
            self._index_record(record)
            cell = self.cells.setdefault(
                (record.product, record.period), _CellState()
            )
            cell.record_ids.append(record.record_id)
        for cell_key, cell in self.cells.items():
            for record_id in cell.record_ids:
                self._recompute_record(record_id, cell)

    def _index_record(self, record) -> None:
        self.record_factor[record.record_id] = record.factor_id
        if record.factor_id:
            self.factor_records.setdefault(record.factor_id, set()).add(
                record.record_id
            )

    def footprints(self) -> Dict[Tuple[str, str], ProductFootprint]:
        return {
            key: cell.to_footprint(*key) for key, cell in sorted(self.cells.items())
        }

    # ------------------------------------------------- per-record evaluation
    def _recompute_record(self, record_id: str, cell: _CellState) -> float:
        """Re-evaluate one record and adjust cell aggregates.

        Returns the emission delta (new emission minus old emission; old
        emission is zero while the record was quarantined).
        """
        record = self.ledger.records[record_id]
        old = self._record_result.get(record_id)
        old_emission = old.emission if isinstance(old, Contribution) else 0.0

        contribution, quarantine = evaluate_record(self.ledger, record)

        # Remove the old classification from the cell.
        if old is not None:
            if isinstance(old, Contribution):
                cell.contributions.pop(record_id, None)
            else:
                cell.quarantined.pop(record_id, None)

        # Install the new classification.
        if contribution is not None:
            cell.contributions[record_id] = contribution
        else:
            assert quarantine is not None
            cell.quarantined[record_id] = quarantine
        self._record_result[record_id] = contribution or quarantine

        new_emission = contribution.emission if contribution is not None else 0.0
        return new_emission - old_emission

    # ------------------------------------------------------ impact analysis
    def _impacted_records(self, events: List[Event]) -> Set[str]:
        impacted: Set[str] = set()
        for event in events:
            p = event.payload
            if event.event_type == "consumption_recorded":
                impacted.add(p["record_id"])
            elif event.event_type in (
                "factor_withdrawn", "factor_version_registered"
            ):
                # Factor change propagates along factor -> records edges.
                impacted.update(self.factor_records.get(p["factor_id"], set()))
            elif event.event_type == "evidence_registered":
                # Evidence change propagates evidence -> factors -> records.
                for factor_id in self.evidence_factors.get(p["evidence_id"], set()):
                    impacted.update(self.factor_records.get(factor_id, set()))
            # report_frozen has no footprint impact
        return impacted

    def apply_events(
        self, events: List[Event], mark_reports: bool = True
    ) -> Tuple[Dict[Tuple[str, str], ProductFootprint], RecomputeStats]:
        # Keep graph indexes in sync with newly applied ledger state.
        for factor in self.ledger.factors.values():
            self.evidence_factors.setdefault(factor.evidence_id, set()).add(
                factor.factor_id
            )
        for event in events:
            if event.event_type == "consumption_recorded":
                record = self.ledger.records[event.payload["record_id"]]
                if record.record_id not in self.record_factor:
                    self._index_record(record)
                    cell = self.cells.setdefault(
                        (record.product, record.period), _CellState()
                    )
                    cell.record_ids.append(record.record_id)

        impacted = self._impacted_records(events)
        impacted_cells: Set[Tuple[str, str]] = set()
        for record_id in sorted(impacted):
            record = self.ledger.records[record_id]
            cell_key = (record.product, record.period)
            # The returned emission delta is the exact cell adjustment; the
            # cell aggregates are mutated in place by _recompute_record.
            self._recompute_record(record_id, self.cells[cell_key])
            impacted_cells.add(cell_key)

        affected_reports: List[str] = []
        causing = tuple(e.event_id for e in events)
        if mark_reports and causing:
            affected_reports = mark_affected_reports(self.ledger, causing)

        stats = RecomputeStats(
            strategy="factor_dependency_graph",
            events_applied=len(events),
            cells_evaluated=len(impacted_cells),
            records_touched=len(impacted),
            factor_versions_touched=len({
                e.payload["factor_id"]
                for e in events
                if e.event_type in ("factor_withdrawn", "factor_version_registered")
            }),
            affected_products=tuple(sorted({p for p, _ in impacted_cells})),
            affected_periods=tuple(sorted({q for _, q in impacted_cells})),
            affected_reports=tuple(affected_reports),
        )
        return self.footprints(), stats
