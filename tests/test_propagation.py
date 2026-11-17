"""Incremental propagation: replacement, withdrawal, drift, recovery, idempotency.

These tests drive a ledger across multiple batches via :class:`LedgerSession`
and assert on both the recomputed numbers and the bounded recompute scope.
"""
import unittest

from carbon_factor_ledger.ledger import Ledger, LedgerError
from carbon_factor_ledger.pipeline import STRATEGIES, LedgerSession
from tests.util import (
    evidence, factor, footprint_by_cell, freeze, parsed, quarantine_map,
    record, withdrawal, h,
)


def session(strategy="materialized"):
    return LedgerSession(strategy)


def base_events():
    return [
        evidence("EV-STEEL"),
        factor("F-STEEL", 1, value=2.0, evidence_id="EV-STEEL",
               valid_from="2026-01-01", valid_to="2026-06-30"),
        factor("F-STEEL", 2, value=1.9, evidence_id="EV-STEEL",
               valid_from="2026-07-01", supersedes=1,
               ts="2026-06-01T00:00:00+00:00"),
        record("R-H1", "P1", quantity=1000, factor_id="F-STEEL",
               occurred_on="2026-03-01"),
        record("R-H2", "P1", quantity=1000, factor_id="F-STEEL",
               occurred_on="2026-08-01"),
        record("R-OTHER", "P2", quantity=10, factor_id="F-STEEL",
               occurred_on="2026-03-01"),
    ]


class TestReplacementScope(unittest.TestCase):
    def test_versioned_replacement_only_affects_its_validity_window(self):
        s = session()
        s.apply(parsed(base_events()))
        cells = footprint_by_cell(s.result())
        # H1 falls in v1 window (2.0), H2 in v2 window (1.9).
        p1 = cells[("P1", "2026-Q1")]
        p1q3 = cells[("P1", "2026-Q3")]
        h1 = next(c for c in p1["contributions"] if c["record_id"] == "R-H1")
        h2 = next(c for c in p1q3["contributions"] if c["record_id"] == "R-H2")
        self.assertEqual(h1["factor_version"], 1)
        self.assertEqual(h1["emission"], 2000.0)
        self.assertEqual(h2["factor_version"], 2)
        self.assertEqual(h2["emission"], 1900.0)

        # Register a corrected v3 replacing v2's window only.
        s.apply(parsed([
            factor("F-STEEL", 3, value=1.5, evidence_id="EV-STEEL",
                   valid_from="2026-07-01", supersedes=2,
                   ts="2026-10-01T00:00:00+00:00"),
        ]))
        cells = footprint_by_cell(s.result())
        h1 = next(c for c in cells[("P1", "2026-Q1")]["contributions"]
                  if c["record_id"] == "R-H1")
        h2 = next(c for c in cells[("P1", "2026-Q3")]["contributions"]
                  if c["record_id"] == "R-H2")
        self.assertEqual(h1["factor_version"], 1)   # v1 window untouched
        self.assertEqual(h1["emission"], 2000.0)
        self.assertEqual(h2["factor_version"], 3)   # v3 takes the later window
        self.assertEqual(h2["emission"], 1500.0)

    def test_incremental_recompute_is_bounded_to_dirty_cells(self):
        s = session("materialized")
        s.apply(parsed(base_events()))
        s.apply(parsed([
            factor("F-OTHER", 1, value=1.0, evidence_id="EV-STEEL",
                   valid_from="2026-01-01"),
            record("R-NEW", "P9", quantity=5, factor_id="F-OTHER",
                   occurred_on="2026-03-01"),
        ]))
        stats = s.last_stats
        # Only P9's new cell is re-evaluated; P1/P2 cached cells are untouched.
        self.assertEqual(stats.cells_evaluated, 1)
        self.assertEqual(stats.affected_products, ("P9",))
        self.assertEqual(stats.records_touched, 1)


class TestWithdrawalPropagation(unittest.TestCase):
    def test_withdrawal_propagates_to_all_dependent_records(self):
        s = session()
        s.apply(parsed(base_events()))
        # Both H1 and R-OTHER resolve to v1; H2 uses v2 and must survive.
        s.apply(parsed([withdrawal("F-STEEL", 1)]))
        stats = s.last_stats
        qmap = quarantine_map(s.result())
        self.assertEqual(set(qmap["R-H1"]), {"FACTOR_WITHDRAWN"})
        self.assertEqual(set(qmap["R-OTHER"]), {"FACTOR_WITHDRAWN"})
        self.assertNotIn("R-H2", qmap)
        cells = footprint_by_cell(s.result())
        self.assertEqual(cells[("P1", "2026-Q3")]["footprint_kgco2e"], 1900.0)
        # Propagation scope is exactly the three records referencing F-STEEL
        # (v1 records get quarantined; the v2 record is re-checked and stays).
        self.assertEqual(stats.records_touched, 3)
        self.assertEqual(stats.cells_evaluated, 3)

    def test_frozen_report_keeps_value_but_is_marked_superseded(self):
        s = session()
        s.apply(parsed(base_events() + [freeze("RP1", "P1", "2026-Q1")]))
        before = s.ledger.reports["RP1"]
        self.assertEqual(before.status, "frozen")
        frozen_value = before.footprint
        s.apply(parsed([withdrawal("F-STEEL", 1)]))
        after = s.ledger.reports["RP1"]
        self.assertEqual(after.status, "superseded")
        self.assertEqual(after.footprint, frozen_value)  # snapshot unchanged
        self.assertEqual(after.contributions, before.contributions)
        self.assertEqual(after.ledger_state_hash, before.ledger_state_hash)
        self.assertTrue(after.superseded_event_ids)

    def test_event_before_freeze_does_not_supersede_report(self):
        s = session()
        s.apply(parsed([
            evidence("EV-X"),
            factor("F-X", 1, value=1.0, evidence_id="EV-X"),
            record("R-X", "P1", quantity=10, factor_id="F-X",
                   occurred_on="2026-01-10"),
            freeze("RP", "P1", "2026-Q1", ts="2026-05-01T00:00:00+00:00"),
        ]))
        # An older back-dated event must not retroactively supersede the report.
        s.apply(parsed([
            record("R-Y", "P1", quantity=1, factor_id="F-X",
                   occurred_on="2026-02-01", ts="2026-02-01T00:00:00+00:00"),
        ]))
        self.assertEqual(s.ledger.reports["RP"].status, "frozen")


class TestEvidenceDrift(unittest.TestCase):
    def test_reimport_with_same_hash_is_idempotent(self):
        s = session()
        events = base_events()
        s.apply(parsed(events))
        hash_before = s.ledger.state_hash
        s.apply(parsed(events))  # replay identical batch
        stats = s.last_stats
        self.assertEqual(s.ledger.state_hash, hash_before)
        self.assertEqual(stats.records_touched, 0)
        self.assertEqual(stats.cells_evaluated, 0)
        inv = {e["evidence_id"]: e for e in s.result()["evidence_inventory"]}
        self.assertEqual(inv["EV-STEEL"]["integrity"], "OK")
        self.assertEqual(inv["EV-STEEL"]["import_count"], 1)

    def test_reimport_with_changed_hash_quarantines_dependent_chain(self):
        s = session()
        s.apply(parsed(base_events()))
        drifted = evidence("EV-STEEL", sha=h("tampered file bytes"),
                           ts="2026-09-02T00:00:00+00:00")
        s.apply(parsed([drifted]))
        qmap = quarantine_map(s.result())
        # Every record depending on a factor attested by EV-STEEL is affected.
        for rid in ("R-H1", "R-H2", "R-OTHER"):
            self.assertIn("EVIDENCE_HASH_MISMATCH", set(qmap[rid]))
        inv = {e["evidence_id"]: e for e in s.result()["evidence_inventory"]}
        self.assertEqual(inv["EV-STEEL"]["integrity"], "CONFLICT")
        # Original attested hash is preserved for the frozen evidence chain.
        self.assertEqual(inv["EV-STEEL"]["sha256"], h("EV-STEEL"))


class TestIdempotencyAndReplay(unittest.TestCase):
    def test_same_version_executed_twice_is_stable(self):
        for strategy in STRATEGIES:
            s1, s2 = LedgerSession(strategy), LedgerSession(strategy)
            events = parsed(base_events())
            s1.apply(events)
            s2.apply(events)
            s2.apply(events)  # duplicate execution
            self.assertEqual(
                s1.ledger.state_hash, s2.ledger.state_hash, strategy
            )
            r1, r2 = s1.result(), s2.result()
            self.assertEqual(r1["totals"], r2["totals"], strategy)

    def test_out_of_order_input_is_deterministic(self):
        events = base_events()
        shuffled = [events[i] for i in (3, 0, 5, 2, 4, 1)]
        sa = LedgerSession()
        sa.apply(parsed(events))
        sb = LedgerSession()
        sb.apply(parsed(shuffled))
        self.assertEqual(sa.ledger.state_hash, sb.ledger.state_hash)
        self.assertEqual(sa.result()["totals"], sb.result()["totals"])

    def test_duplicate_factor_content_is_noop_conflicting_content_rejected(self):
        s = LedgerSession()
        s.apply(parsed([
            evidence("EV-1"),
            factor("F1", 1, value=2.0, evidence_id="EV-1"),
        ]))
        # Identical re-registration: idempotent.
        batch = s.apply(parsed([
            factor("F1", 1, value=2.0, evidence_id="EV-1"),
        ]))
        self.assertEqual(batch.rejected, ())
        # Changed content under the same id/version: rejected, not overwritten.
        batch = s.apply(parsed([
            factor("F1", 1, value=9.9, evidence_id="EV-1"),
        ]))
        self.assertEqual(len(batch.rejected), 1)
        self.assertEqual(s.ledger.get_factor("F1", 1).factor_value, 2.0)


class TestFailureRecovery(unittest.TestCase):
    def test_failed_batch_rolls_back_entire_transaction(self):
        s = LedgerSession()
        s.apply(parsed(base_events()))
        hash_before = s.ledger.state_hash
        good = record("R-OK", "P3", quantity=7, factor_id="F-STEEL",
                      occurred_on="2026-03-01",
                      ts="2026-07-01T00:00:00+00:00")
        bad = withdrawal("F-GHOST", 9)  # unknown factor -> LedgerError
        batch = s.apply(parsed([good, bad]))
        # The good event in the same batch is rolled back too.
        self.assertEqual(s.ledger.state_hash, hash_before)
        self.assertNotIn("R-OK", s.ledger.records)
        self.assertEqual(batch.accepted, 1)
        self.assertEqual(len(batch.rejected), 1)
        # Ledger remains usable after recovery.
        s.apply(parsed([good]))
        self.assertIn("R-OK", s.ledger.records)

    def test_rebuild_from_event_log_recovers_identical_state(self):
        s1 = LedgerSession()
        s1.apply(parsed(base_events()))
        s1.apply(parsed([withdrawal("F-STEEL", 1)]))
        # Simulate recovery: replay the durable event log into a fresh ledger.
        s2 = LedgerSession()
        s2.apply(list(s1.ledger.events))
        self.assertEqual(s1.ledger.state_hash, s2.ledger.state_hash)
        self.assertEqual(s1.result()["totals"], s2.result()["totals"])


class TestStrategyEquivalence(unittest.TestCase):
    def test_incremental_batches_converge_across_strategies(self):
        sessions = {name: LedgerSession(name) for name in STRATEGIES}
        batches = [
            base_events(),
            [withdrawal("F-STEEL", 1)],
            [evidence("EV-STEEL", sha=h("tampered"),
                     ts="2026-09-03T00:00:00+00:00")],
            [record("R-LATE", "P1", quantity=3, factor_id="F-STEEL",
                    occurred_on="2026-08-15")],
        ]
        scopes = []
        for batch in batches:
            parsed_batch = parsed(batch)
            for name, s in sessions.items():
                s.apply(parsed_batch)
            scopes.append({
                name: sessions[name].last_stats.cells_evaluated
                for name in STRATEGIES
            })
        totals = {name: s.result()["totals"] for name, s in sessions.items()}
        self.assertEqual(totals["materialized"], totals["graph"])
        self.assertEqual(totals["materialized"], totals["report_time"])
        hashes = {s.ledger.state_hash for s in sessions.values()}
        self.assertEqual(len(hashes), 1)
        # Incremental strategies evaluate fewer cells than full recompute once
        # the initial load is done; report_time always scans every cell.
        for scope in scopes[1:]:
            self.assertLessEqual(scope["graph"], scope["report_time"])
            self.assertLessEqual(scope["materialized"], scope["report_time"])


if __name__ == "__main__":
    unittest.main()
