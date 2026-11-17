"""Strategy A — per-product materialized footprint store with incremental refresh.

Footprints are precomputed per (product, period) cell and cached.  When new
events arrive only the *dirty* cells are re-evaluated; all other cells keep
their cached result.  This bounds recomputation to the products actually
touched by an import and keeps temporary state to one cell at a time.

Correctness anchor: the refresh math is exactly
:func:`carbon_factor_ledger.engine.evaluate_cell`, so a full refresh and an
incremental refresh over the same ledger state produce identical footprints.
"""
from __future__ import annotations

from typing import Dict, List, Set, Tuple

from .engine import evaluate_cell, mark_affected_reports
from .ledger import Ledger
from .models import Event, ProductFootprint, RecomputeStats


class MaterializedStore:
    def __init__(self, ledger: Ledger) -> None:
        self.ledger = ledger
        self._cache: Dict[Tuple[str, str], ProductFootprint] = {}
        self.dirty: Set[Tuple[str, str]] = set()

    # ------------------------------------------------------------- building
    def load_full(self) -> None:
        """Initial materialization: every cell once."""
        self._cache = {
            cell: evaluate_cell(self.ledger, *cell).to_footprint()
            for cell in self.ledger.all_cells()
        }
        self.dirty = set()

    def footprints(self) -> Dict[Tuple[str, str], ProductFootprint]:
        if self.dirty:  # pragma: no cover - programming guard, not user state
            raise RuntimeError("dirty cells must be refreshed before reading")
        return dict(self._cache)

    # --------------------------------------------------------- dirty routing
    def _cells_for_event(self, event: Event) -> Set[Tuple[str, str]]:
        p = event.payload
        if event.event_type == "consumption_recorded":
            occurred = self.ledger.records[p["record_id"]].occurred_on
            period = p.get("period") or (
                f"{occurred.year}-Q{((occurred.month - 1) // 3) + 1}"
            )
            return {(p["product"], period)}
        if event.event_type in ("factor_withdrawn", "factor_version_registered"):
            factor_id = p["factor_id"]
            return {
                (r.product, r.period)
                for r in self.ledger.records.values()
                if r.factor_id == factor_id
            }
        if event.event_type == "evidence_registered":
            evidence_id = p["evidence_id"]
            factor_ids = {
                f.factor_id
                for f in self.ledger.factors.values()
                if f.evidence_id == evidence_id
            }
            return {
                (r.product, r.period)
                for r in self.ledger.records.values()
                if r.factor_id in factor_ids
            }
        return set()  # report_frozen does not change live footprints

    # -------------------------------------------------------------- refresh
    def apply_events(
        self, events: List[Event], mark_reports: bool = True
    ) -> Tuple[Dict[Tuple[str, str], ProductFootprint], RecomputeStats]:
        for event in events:
            if event.event_type == "report_frozen":
                continue
            self.dirty.update(self._cells_for_event(event))

        touched_records = set()
        for product, period in self.dirty:
            for record in self.ledger.cell_records(product, period):
                touched_records.add(record.record_id)
            # Re-evaluate only the dirty cell; the rest of the cache is untouched.
            self._cache[(product, period)] = evaluate_cell(
                self.ledger, product, period
            ).to_footprint()

        affected_reports: List[str] = []
        causing = tuple(e.event_id for e in events)
        if mark_reports and causing:
            affected_reports = mark_affected_reports(self.ledger, causing)

        dirty_snapshot = sorted(self.dirty)
        stats = RecomputeStats(
            strategy="materialized_per_product",
            events_applied=len(events),
            cells_evaluated=len(dirty_snapshot),
            records_touched=len(touched_records),
            factor_versions_touched=len({
                e.payload["factor_id"]
                for e in events
                if e.event_type in ("factor_withdrawn", "factor_version_registered")
            }),
            affected_products=tuple(sorted({p for p, _ in dirty_snapshot})),
            affected_periods=tuple(sorted({q for _, q in dirty_snapshot})),
            affected_reports=tuple(affected_reports),
        )
        self.dirty = set()
        return dict(self._cache), stats
