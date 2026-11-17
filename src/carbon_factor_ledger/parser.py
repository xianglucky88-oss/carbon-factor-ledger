"""Input parsing, validation and canonical event extraction.

The ledger is event-sourced: the payload is an unordered (or partially ordered)
list of events.  Parsing is pure and deterministic:

* every event is validated and normalized;
* an event without an ``event_id`` gets a content-addressed id (SHA-256 of its
  canonical JSON), so importing the same file twice is idempotent;
* events are returned in a total order (``ingest_seq`` then canonical id),
  which makes out-of-order input reproducible regardless of list order.
"""
from __future__ import annotations

import hashlib
import json
from datetime import date, datetime
from typing import Any, Dict, List, Tuple

from .models import EVENT_TYPES, Event


class EventValidationError(ValueError):
    pass


# ---------------------------------------------------------------------------
# Deterministic helpers
# ---------------------------------------------------------------------------


def canonical_json(obj: Any) -> str:
    """JSON serialization with stable key ordering and no whitespace drift."""
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def content_hash(obj: Any) -> str:
    return hashlib.sha256(canonical_json(obj).encode("utf-8")).hexdigest()


def parse_date(value: Any, field_name: str) -> date:
    if isinstance(value, date) and not isinstance(value, datetime):
        return value
    if isinstance(value, datetime):
        return value.date()
    if not isinstance(value, str):
        raise EventValidationError(f"{field_name} must be an ISO date string")
    try:
        return date.fromisoformat(value[:10])
    except ValueError as exc:
        raise EventValidationError(f"{field_name} is not a valid ISO date: {value!r}") from exc


def parse_timestamp(value: Any, field_name: str = "timestamp") -> datetime:
    if isinstance(value, datetime):
        return value
    if not isinstance(value, str):
        raise EventValidationError(f"{field_name} must be an ISO-8601 string")
    text = value.replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(text)
    except ValueError as exc:
        raise EventValidationError(f"{field_name} is not a valid timestamp: {value!r}") from exc


def _require(payload: Dict[str, Any], key: str, event_type: str) -> Any:
    if key not in payload or payload[key] in (None, ""):
        raise EventValidationError(f"{event_type} requires field {key!r}")
    return payload[key]


# ---------------------------------------------------------------------------
# Per-event validation (structure only; referential rules live in the ledger)
# ---------------------------------------------------------------------------


def _validate(event_type: str, p: Dict[str, Any]) -> Dict[str, Any]:
    if event_type == "evidence_registered":
        _require(p, "evidence_id", event_type)
        _require(p, "filename", event_type)
        h = str(_require(p, "sha256", event_type)).lower()
        if len(h) != 64 or any(c not in "0123456789abcdef" for c in h):
            raise EventValidationError("sha256 must be 64 hex characters")
        p = {**p, "sha256": h}
    elif event_type == "factor_version_registered":
        for key in (
            "factor_id", "version", "activity", "material", "factor_value",
            "factor_unit", "geography", "boundary", "valid_from", "evidence_id",
        ):
            _require(p, key, event_type)
        if not isinstance(p["version"], int) or p["version"] < 1:
            raise EventValidationError("factor version must be a positive integer")
        float(p["factor_value"])
        parse_date(p["valid_from"], "valid_from")
        if p.get("valid_to") is not None:
            parse_date(p["valid_to"], "valid_to")
    elif event_type == "factor_withdrawn":
        _require(p, "factor_id", event_type)
        if not isinstance(_require(p, "version", event_type), int):
            raise EventValidationError("withdrawn event requires integer version")
    elif event_type == "consumption_recorded":
        for key in (
            "record_id", "product", "material", "activity", "quantity",
            "unit", "geography", "boundary", "occurred_on",
        ):
            _require(p, key, event_type)
        float(p["quantity"])
        parse_date(p["occurred_on"], "occurred_on")
        if p.get("factor_version") is not None and not isinstance(p["factor_version"], int):
            raise EventValidationError("factor_version must be an integer or null")
    elif event_type == "report_frozen":
        for key in ("report_id", "product", "period"):
            _require(p, key, event_type)
    else:  # pragma: no cover - guarded by caller
        raise EventValidationError(f"unknown event type: {event_type}")
    return p


def _dependencies(event: "Event") -> set:
    """Entity keys an event depends on (which must therefore exist first)."""
    p = event.payload
    deps = set()
    if event.event_type == "factor_version_registered":
        deps.add(("evidence", p["evidence_id"]))
    elif event.event_type == "factor_withdrawn":
        deps.add(("factor", (p["factor_id"], p["version"])))
    elif event.event_type == "consumption_recorded":
        if p.get("factor_id"):
            deps.add(("factor_id", p["factor_id"]))
    elif event.event_type == "evidence_registered":
        # Re-imports of the same identity are independent of the first import;
        # the ledger handles both orderings idempotently.
        pass
    return deps


def _entity_keys(event: "Event") -> set:
    """Entity keys an event creates or updates."""
    p = event.payload
    if event.event_type == "evidence_registered":
        return {("evidence", p["evidence_id"])}
    if event.event_type == "factor_version_registered":
        return {
            ("factor", (p["factor_id"], p["version"])),
            ("factor_id", p["factor_id"]),
        }
    return set()


def _topological_order(events: List["Event"]) -> List[Event]:
    """Deterministic ordering that respects entity dependencies.

    Kahn's algorithm over the event-level dependency graph, with event-id
    tie-breaking.  This yields one canonical order for any permutation of the
    same input (out-of-order determinism) while guaranteeing evidence is
    registered before factors, factors before withdrawals/records, and freeze
    snapshots last.  Cycles cannot occur because edges only flow from a
    referenced entity to its creating event.  Duplicate events (identical
    content ids, e.g. repeated imports) are de-duplicated first.
    """
    # De-duplicate by content id while preserving one representative each.
    unique: Dict[str, "Event"] = {}
    for event in events:
        unique.setdefault(event.event_id, event)
    events = list(unique.values())

    producers: Dict[Any, List[str]] = {}
    for event in events:
        for key in _entity_keys(event):
            producers.setdefault(key, []).append(event.event_id)

    # indegree: number of distinct producing events not yet emitted
    indegree: Dict[str, int] = {}
    dependents: Dict[str, set] = {}
    for event in events:
        prerequisite_ids: set = set()
        for key in _dependencies(event):
            for pid in producers.get(key, []):
                if pid != event.event_id:
                    prerequisite_ids.add(pid)
        indegree[event.event_id] = len(prerequisite_ids)
        for pid in prerequisite_ids:
            dependents.setdefault(pid, set()).add(event.event_id)

    ready = sorted(eid for eid, deg in indegree.items() if deg == 0)
    ordered: List[Event] = []
    while ready:
        eid = ready.pop(0)
        ordered.append(unique[eid])
        for dependent in sorted(dependents.get(eid, set())):
            indegree[dependent] -= 1
            if indegree[dependent] == 0:
                ready.append(dependent)
                ready.sort()
    if len(ordered) != len(events):  # pragma: no cover - defensive
        raise EventValidationError("cyclic event dependencies detected")

    # Freeze events move to the end while keeping deterministic relative order
    # so a snapshot sees the complete state present at freeze time.
    non_freeze = [e for e in ordered if e.event_type != "report_frozen"]
    freeze = [e for e in ordered if e.event_type == "report_frozen"]
    return non_freeze + sorted(freeze, key=lambda e: e.event_id)


def parse_events(payload: Any) -> Tuple[List[Event], List[Tuple[int, str]]]:
    """Parse a raw payload into an ordered list of events.

    Returns ``(events, parse_errors)``.  Malformed events are reported rather
    than aborting the whole import (one bad row must not roll back the valid
    rows); callers decide whether the batch is acceptable.

    Ordering is a deterministic topological sort over entity dependencies with
    event-id tie-breaks, so shuffling the input list cannot change the
    resulting ledger state.
    """
    if isinstance(payload, dict) and "events" in payload:
        raw_events = payload["events"]
    elif isinstance(payload, list):
        raw_events = payload
    else:
        raise EventValidationError(
            "payload must be a list of events or {'events': [...]}"
        )
    if not isinstance(raw_events, list):
        raise EventValidationError("events must be a list")

    parsed: List[Event] = []
    errors: List[Tuple[int, str]] = []
    for ingest_seq, raw in enumerate(raw_events):
        if not isinstance(raw, dict):
            errors.append((ingest_seq, "event must be an object"))
            continue
        event_type = raw.get("event_type")
        if event_type not in EVENT_TYPES:
            errors.append((ingest_seq, f"unknown event_type: {event_type!r}"))
            continue
        try:
            body = dict(raw.get("payload", raw))
            body = _validate(event_type, body)
            ts = parse_timestamp(raw.get("timestamp", "1970-01-01T00:00:00+00:00"))
            provided_id = raw.get("event_id")
            event_id = str(provided_id) if provided_id else content_hash(
                {"event_type": event_type, "payload": body}
            )
            parsed.append(
                Event(seq=ingest_seq, event_id=event_id, event_type=event_type,
                      timestamp=ts, payload=body)
            )
        except EventValidationError as exc:
            errors.append((ingest_seq, str(exc)))

    # Deterministic dependency ordering (see _topological_order).
    return _topological_order(parsed), errors
