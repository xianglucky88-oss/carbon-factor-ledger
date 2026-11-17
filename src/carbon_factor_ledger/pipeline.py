"""Orchestration boundary: parse -> transactional apply -> bounded recompute.

Three recomputation strategies share the same ledger and per-record math and
are selected by name:

* ``materialized`` — per-product materialized store, dirty-cell refresh;
* ``graph``        — factor dependency graph, edge-based propagation with
                     per-record emission deltas;
* ``report_time``  — full recompute at read time (no maintained state).

A :class:`LedgerSession` owns the ledger plus, for the stateful strategies,
their maintained state.  Follow-up batches (factor replacement, withdrawal,
evidence re-import) go through :meth:`apply`, which demonstrates the bounded
recompute scope via :class:`RecomputeStats`.  All three strategies evaluate
through the same applicability/engine code, so they can only differ in *which*
cells they revisit, never in the numbers they produce.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

from .engine import full_recompute
from .graph import DependencyGraph
from .ledger import Ledger
from .materialized import MaterializedStore
from .models import BatchResult, Event, ProductFootprint, RecomputeStats
from .parser import parse_events
from .report import build_result

PROJECT = "carbon-factor-ledger"
STRATEGIES = ("materialized", "graph", "report_time")


class LedgerSession:
    def __init__(self, strategy: str = "materialized",
                 ledger: Optional["Ledger"] = None) -> None:
        if strategy not in STRATEGIES:
            raise ValueError(
                f"unknown strategy {strategy!r}; choose one of {STRATEGIES}"
            )
        self.strategy = strategy
        self.ledger = ledger or Ledger()
        self._store: Optional[MaterializedStore] = (
            MaterializedStore(self.ledger) if strategy == "materialized" else None
        )
        self._graph: Optional[DependencyGraph] = (
            DependencyGraph(self.ledger) if strategy == "graph" else None
        )
        self.footprints: Dict[Tuple[str, str], ProductFootprint] = {}
        self.last_stats: Optional[RecomputeStats] = None
        self.last_batch: Optional[BatchResult] = None
        self._initialized = False

    # ------------------------------------------------------------- writing
    def apply(self, events: List[Event]) -> BatchResult:
        """Apply one transactional batch and recompute within a bounded scope."""
        initial_load = not self._initialized
        known_before = set(self.ledger._event_ids)
        batch = self.ledger.apply_batch(events)
        # Only events that actually entered the event log can change state.
        # Idempotent re-imports are filtered here so they cost zero recompute.
        applied = [
            e for e in events
            if e.event_id not in known_before and self.ledger.has_event(e.event_id)
        ]

        if self.strategy == "report_time":
            # On the initial load every applied event predates (or is) the
            # freeze, so no report can be superseded yet.
            self.footprints, self.last_stats = full_recompute(
                self.ledger,
                events_applied=len(applied),
                causing_event_ids=tuple(e.event_id for e in applied),
                mark_reports=not initial_load,
            )
        elif self.strategy == "graph":
            assert self._graph is not None
            if not self._graph.cells:
                self._graph.load_full()
            elif batch.rejected:
                # Transactional rollback: rebuild graph view from ledger.
                self._graph.load_full()
            self.footprints, self.last_stats = self._graph.apply_events(
                applied, mark_reports=not initial_load
            )
        else:
            assert self._store is not None
            if not self._store._cache:
                self._store.load_full()
            elif batch.rejected:
                # Transactional rollback: rebuild cache from ledger.
                self._store.load_full()
            self.footprints, self.last_stats = self._store.apply_events(
                applied, mark_reports=not initial_load
            )

        self._initialized = True
        self.last_batch = batch
        return batch
    # ------------------------------------------------------------- reading
    def result(self, parse_errors: Optional[List[tuple]] = None) -> Dict[str, Any]:
        if self.last_stats is None:
            self.last_stats = RecomputeStats(
                strategy=self.strategy,
                events_applied=0, cells_evaluated=0, records_touched=0,
                factor_versions_touched=0,
                affected_products=(), affected_periods=(), affected_reports=(),
            )
        if self.last_batch is None:
            self.last_batch = BatchResult(accepted=0, rejected=(),
                                          state_hash=self.ledger.state_hash)
        return build_result(
            PROJECT,
            strategy=self.strategy,
            ledger=self.ledger,
            footprints=self.footprints,
            stats=self.last_stats,
            batch=self.last_batch,
            parse_errors=parse_errors or [],
        )


def run(payload: Dict[str, Any], strategy: str = "materialized") -> Dict[str, Any]:
    """One-shot entry point: parse a payload and apply it to a fresh session."""
    events, parse_errors = parse_events(payload)
    session = LedgerSession(strategy=strategy)
    session.apply(events)
    return session.result(parse_errors=parse_errors)
