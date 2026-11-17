"""Shared builders for deterministic, dependency-free test events."""
from __future__ import annotations

import hashlib
from typing import Any, Dict, List, Optional

from carbon_factor_ledger.parser import canonical_json, content_hash, parse_events


def h(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def ev(event_type: str, payload: Dict[str, Any], timestamp: str,
       event_id: Optional[str] = None) -> Dict[str, Any]:
    event: Dict[str, Any] = {"event_type": event_type, "payload": payload,
                             "timestamp": timestamp}
    if event_id:
        event["event_id"] = event_id
    return event


def evidence(eid: str, filename: str = None, sha: str = None,
             ts: str = "2025-01-01T00:00:00+00:00") -> Dict[str, Any]:
    return ev("evidence_registered", {
        "evidence_id": eid,
        "filename": filename or f"{eid}.pdf",
        "sha256": sha or h(eid),
    }, ts)


def factor(fid: str, version: int, *, value: float, unit: str = "kgCO2e/kg",
           geography: str = "CN", boundary: str = "CRADLE_TO_GATE",
           valid_from: str = "2025-01-01", valid_to: Optional[str] = None,
           evidence_id: str = None, activity: str = "material_input",
           material: str = "steel", supersedes: Optional[int] = None,
           ts: str = "2025-01-02T00:00:00+00:00") -> Dict[str, Any]:
    payload: Dict[str, Any] = {
        "factor_id": fid, "version": version, "activity": activity,
        "material": material, "factor_value": value, "factor_unit": unit,
        "geography": geography, "boundary": boundary,
        "valid_from": valid_from, "valid_to": valid_to,
        "evidence_id": evidence_id or f"EV-{fid}",
    }
    if supersedes is not None:
        payload["supersedes"] = supersedes
    return ev("factor_version_registered", payload, ts)


def withdrawal(fid: str, version: int,
               ts: str = "2026-09-01T00:00:00+00:00") -> Dict[str, Any]:
    return ev("factor_withdrawn", {"factor_id": fid, "version": version}, ts)


def record(rid: str, product: str, *, quantity: float, unit: str = "kg",
           factor_id: Optional[str] = "F1",
           factor_version: Optional[int] = None,
           material: str = "steel", activity: str = "material_input",
           geography: str = "CN", boundary: str = "CRADLE_TO_GATE",
           occurred_on: str = "2026-01-15", period: Optional[str] = None,
           ts: str = "2026-01-15T09:00:00+00:00") -> Dict[str, Any]:
    payload: Dict[str, Any] = {
        "record_id": rid, "product": product, "material": material,
        "activity": activity, "quantity": quantity, "unit": unit,
        "geography": geography, "boundary": boundary,
        "occurred_on": occurred_on, "factor_id": factor_id,
    }
    if factor_version is not None:
        payload["factor_version"] = factor_version
    if period:
        payload["period"] = period
    return ev("consumption_recorded", payload, ts)


def freeze(report_id: str, product: str, period: str,
           ts: str = "2026-02-01T12:00:00+00:00") -> Dict[str, Any]:
    return ev("report_frozen", {"report_id": report_id, "product": product,
                                "period": period}, ts)


def parsed(events: List[Dict[str, Any]]):
    parsed_events, errors = parse_events({"events": events})
    assert not errors, errors
    return parsed_events


def footprint_by_cell(result: Dict[str, Any]):
    return {(f["product"], f["period"]): f for f in result["product_footprints"]}


def quarantine_map(result: Dict[str, Any]):
    out: Dict[str, List[str]] = {}
    for fp in result["product_footprints"]:
        for q in fp["quarantined"]:
            out[q["record_id"]] = list(q["reasons"])
    return out


def contribution_map(result: Dict[str, Any]):
    out: Dict[str, Dict[str, Any]] = {}
    for fp in result["product_footprints"]:
        for c in fp["contributions"]:
            out[c["record_id"]] = c
    return out


def event_id_of(event: Dict[str, Any]) -> str:
    return content_hash({
        "event_type": event["event_type"], "payload": event["payload"]
    })
