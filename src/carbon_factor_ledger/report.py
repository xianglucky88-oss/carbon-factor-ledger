"""Stable, deterministic output envelope and serialization."""
from __future__ import annotations

from dataclasses import asdict
from datetime import date, datetime
from typing import Any, Dict, List

from .models import (
    BatchResult,
    FactorVersion,
    FrozenReport,
    ProductFootprint,
    RecomputeStats,
)


def _json_default(obj: Any) -> Any:
    if isinstance(obj, (datetime, date)):
        return obj.isoformat()
    raise TypeError(f"not JSON serializable: {type(obj)!r}")


def factor_to_dict(factor: FactorVersion) -> Dict[str, Any]:
    return {
        "factor_id": factor.factor_id,
        "version": factor.version,
        "activity": factor.activity,
        "material": factor.material,
        "factor_value": factor.factor_value,
        "factor_unit": f"{factor.factor_unit_numerator}/{factor.reference_unit}",
        "geography": factor.geography,
        "boundary": factor.boundary,
        "valid_from": factor.valid_from.isoformat(),
        "valid_to": factor.valid_to.isoformat() if factor.valid_to else None,
        "evidence_id": factor.evidence_id,
        "supersedes": factor.supersedes,
        "status": factor.status,
        "withdrawn_at": factor.withdrawn_at.isoformat() if factor.withdrawn_at else None,
        "registered_at": factor.registered_at.isoformat(),
    }


def footprint_to_dict(fp: ProductFootprint) -> Dict[str, Any]:
    return {
        "product": fp.product,
        "period": fp.period,
        "footprint_kgco2e": fp.footprint,
        "status": fp.status,
        "coverage_ratio": fp.coverage,
        "included_records": fp.included_count,
        "quarantined_records": fp.quarantined_count,
        "evidence_refs": list(fp.evidence_refs),
        "contributions": [asdict(c) for c in sorted(fp.contributions, key=lambda c: c.record_id)],
        "quarantined": [asdict(q) for q in sorted(fp.quarantined, key=lambda q: q.record_id)],
    }


def report_to_dict(report: FrozenReport) -> Dict[str, Any]:
    return {
        "report_id": report.report_id,
        "product": report.product,
        "period": report.period,
        "status": report.status,
        "frozen_at": report.frozen_at.isoformat(),
        "superseded_at": report.superseded_at.isoformat() if report.superseded_at else None,
        "superseded_by_event_ids": list(report.superseded_event_ids),
        "ledger_state_hash": report.ledger_state_hash,
        "footprint_kgco2e": report.footprint,
        "included_records": report.included_count,
        "quarantined_records": report.quarantined_count,
        "frozen_contributions": [asdict(c) for c in report.contributions],
        "frozen_quarantined": [asdict(q) for c in [report.quarantined] for q in c],
    }


def stats_to_dict(stats: RecomputeStats) -> Dict[str, Any]:
    return asdict(stats)


def build_result(
    project: str,
    *,
    strategy: str,
    ledger,
    footprints: Dict[Any, ProductFootprint],
    stats: RecomputeStats,
    batch: BatchResult,
    parse_errors: List[tuple],
) -> Dict[str, Any]:
    fp_list = [footprint_to_dict(fp) for _key, fp in sorted(footprints.items())]
    totals = {
        "footprint_kgco2e": round(sum(fp.footprint for fp in footprints.values()), 6),
        "included_records": sum(fp.included_count for fp in footprints.values()),
        "quarantined_records": sum(fp.quarantined_count for fp in footprints.values()),
        "quarantine_reasons": _quarantine_reason_counts(footprints),
    }
    overall_status = (
        "INCOMPLETE"
        if any(fp.status == "INCOMPLETE" for fp in footprints.values())
        else "COMPLETE"
    )
    return {
        "project": project,
        "status": "ok",
        "overall_status": overall_status,
        "strategy": strategy,
        "ledger_state_hash": ledger.state_hash,
        "events_applied": len(ledger.events),
        "batch": {
            "accepted": batch.accepted,
            "rejected": [
                {"event_id": eid, "error": err} for eid, err in batch.rejected
            ],
        },
        "parse_errors": [
            {"index": idx, "error": err} for idx, err in parse_errors
        ],
        "evidence_inventory": _evidence_inventory(ledger),
        "factor_versions": [
            factor_to_dict(f) for _key, f in sorted(ledger.factors.items())
        ],
        "totals": totals,
        "product_footprints": fp_list,
        "frozen_reports": [
            report_to_dict(r) for _rid, r in sorted(ledger.reports.items())
        ],
        "recompute_stats": stats_to_dict(stats),
    }


def _quarantine_reason_counts(footprints: Dict[Any, ProductFootprint]) -> Dict[str, int]:
    counts: Dict[str, int] = {}
    for fp in footprints.values():
        for q in fp.quarantined:
            for reason in q.reasons:
                counts[reason] = counts.get(reason, 0) + 1
    return dict(sorted(counts.items()))


def _evidence_inventory(ledger) -> List[Dict[str, Any]]:
    out = []
    for eid in sorted(ledger.evidence):
        state = ledger._evidence_state[eid]
        evidence = ledger.evidence[eid]
        out.append({
            "evidence_id": eid,
            "filename": evidence.filename,
            "sha256": evidence.sha256,
            "import_count": state["imports"],
            "hash_conflicts": sorted(state["conflicts"]),
            "integrity": "CONFLICT" if state["conflicts"] else "OK",
        })
    return out
