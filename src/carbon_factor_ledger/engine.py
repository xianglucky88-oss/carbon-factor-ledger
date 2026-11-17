"""Domain calculation: evaluate records against ledger state.

This module holds no state.  It provides:

* :func:`evaluate_cell` — classify every record of one (product, period) cell;
* :func:`evaluate_all` — evaluate all cells plus frozen-report status;
* :func:`full_recompute` — the *report-time recompute* strategy: rebuild every
  footprint from the current ledger on demand.

The other two strategies (materialized, graph) live in their own modules and
delegate the per-cell math back here, so all three are guaranteed to share one
calculation implementation and can only differ in *what* they recompute.
"""
from __future__ import annotations

from typing import Dict, List, Optional, Tuple

from .applicability import evaluate_record
from .ledger import Ledger
from .models import (
    Evaluation,
    ProductFootprint,
    RecomputeStats,
)


def evaluate_cell(ledger: Ledger, product: str, period: str) -> Evaluation:
    contributions = []
    quarantined = []
    for record in ledger.cell_records(product, period):
        contribution, quarantine = evaluate_record(ledger, record)
        if contribution is not None:
            contributions.append(contribution)
        else:
            assert quarantine is not None
            quarantined.append(quarantine)
    return Evaluation(
        product=product,
        period=period,
        contributions=tuple(contributions),
        quarantined=tuple(quarantined),
    )


def evaluate_all(ledger: Ledger) -> Dict[Tuple[str, str], ProductFootprint]:
    return {
        cell: evaluate_cell(ledger, *cell).to_footprint()
        for cell in ledger.all_cells()
    }


def mark_affected_reports(
    ledger: Ledger,
    causing_event_ids: Tuple[str, ...],
) -> List[str]:
    """Mark frozen reports superseded by post-freeze changes.

    A report is affected only when (a) a causing event landed *after* it was
    frozen and (b) the event touches a record in the report's cell, directly
    or via factor/evidence propagation.  The stored snapshot — footprint,
    contributions, evidence hashes — is never modified; only the
    ``superseded`` marker and the causing event ids change.
    """
    events_by_id = {e.event_id: e for e in ledger.events}
    causing = [events_by_id[eid] for eid in causing_event_ids if eid in events_by_id]
    changed_records = set(_records_from_causing_events(ledger, causing_event_ids))

    affected: List[str] = []
    for report in ledger.reports.values():
        post_freeze = [
            e for e in causing if e.timestamp > report.frozen_at
        ]
        if not post_freeze:
            continue
        cell_record_ids = {
            r.record_id for r in ledger.cell_records(report.product, report.period)
        }
        if not (changed_records & cell_record_ids):
            continue
        ledger.mark_report_superseded(
            report.report_id,
            max(e.timestamp for e in post_freeze),
            tuple(sorted(e.event_id for e in post_freeze)),
        )
        affected.append(report.report_id)
    return sorted(affected)


def full_recompute(
    ledger: Ledger,
    events_applied: int,
    causing_event_ids: Tuple[str, ...] = (),
    mark_reports: bool = True,
) -> Tuple[Dict[Tuple[str, str], ProductFootprint], RecomputeStats]:
    """Strategy C — recompute everything from the current ledger state.

    Scope is intentionally the whole ledger: this is the baseline against which
    the incremental strategies are compared.  No intermediate state is kept
    beyond the returned footprints.  ``mark_reports`` is disabled for an
    initial load (a report cannot be superseded by events that predate it) and
    enabled for follow-up batches.
    """
    footprints = evaluate_all(ledger)

    affected_reports: List[str] = []
    if mark_reports and causing_event_ids:
        affected_reports = mark_affected_reports(ledger, causing_event_ids)

    stats = RecomputeStats(
        strategy="report_time_recompute",
        events_applied=events_applied,
        cells_evaluated=len(footprints),
        records_touched=len(ledger.records),
        factor_versions_touched=len(ledger.factors),
        affected_products=tuple(sorted({p for p, _ in footprints})),
        affected_periods=tuple(sorted({q for _, q in footprints})),
        affected_reports=tuple(affected_reports),
    )
    return footprints, stats


def _records_from_causing_events(
    ledger: Ledger, causing_event_ids: Tuple[str, ...]
) -> List[str]:
    ids = set(causing_event_ids)
    out: List[str] = []
    for record in ledger.records.values():
        if record.event_id in ids:
            out.append(record.record_id)
    # Factor-side events propagate through every record referencing the factor.
    for event in ledger.events:
        if event.event_id not in ids:
            continue
        if event.event_type in ("factor_withdrawn", "factor_version_registered"):
            fid = event.payload["factor_id"]
            for record in ledger.records.values():
                if record.factor_id == fid:
                    out.append(record.record_id)
        elif event.event_type == "evidence_registered":
            eid = event.payload["evidence_id"]
            for factor in ledger.factors.values():
                if factor.evidence_id == eid:
                    for record in ledger.records.values():
                        if record.factor_id == factor.factor_id:
                            out.append(record.record_id)
    return sorted(set(out))
