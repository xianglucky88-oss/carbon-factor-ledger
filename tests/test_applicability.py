"""Applicability rules: every quarantine reason plus multi-reason accumulation."""
import unittest

from carbon_factor_ledger.ledger import Ledger
from carbon_factor_ledger.pipeline import LedgerSession
from tests.util import (
    evidence, factor, footprint_by_cell, parsed, quarantine_map, record,
    withdrawal,
)


def build(events):
    session = LedgerSession("materialized")
    session.apply(parsed(events))
    return session


class TestApplicability(unittest.TestCase):
    BASE = [
        evidence("EV-F1"),
        factor("F1", 1, value=2.0, evidence_id="EV-F1",
               valid_from="2026-01-01", valid_to="2026-06-30"),
    ]

    def _run(self, extra):
        return build(self.BASE + extra).result()

    def test_happy_path_contribution_carries_evidence_hash(self):
        result = self._run([record("R1", "P1", quantity=10)])
        cells = footprint_by_cell(result)
        fp = cells[("P1", "2026-Q1")]
        self.assertEqual(fp["status"], "COMPLETE")
        self.assertEqual(fp["footprint_kgco2e"], 20.0)
        c = fp["contributions"][0]
        self.assertEqual(c["emission"], 20.0)
        self.assertEqual(c["evidence_id"], "EV-F1")
        # The contribution pins the exact attested evidence hash.
        self.assertEqual(len(c["evidence_sha256"]), 64)

    def test_missing_evidence_is_quarantined_not_zero(self):
        events = [
            factor("F1", 1, value=2.0, evidence_id="EV-GHOST"),
            record("R1", "P1", quantity=10),
        ]
        result = build(events).result()
        qmap = quarantine_map(result)
        self.assertEqual(qmap["R1"], ["EVIDENCE_MISSING"])
        self.assertEqual(
            footprint_by_cell(result)[("P1", "2026-Q1")]["footprint_kgco2e"], 0.0
        )
        # The 10 kg are still visible; they were not silently dropped.
        q = footprint_by_cell(result)[("P1", "2026-Q1")]["quarantined"][0]
        self.assertEqual(q["quantity"], 10.0)

    def test_expired_factor_vs_unknown_factor_are_distinct(self):
        events = [
            evidence("EV-OLD"),
            factor("F-OLD", 1, value=1.0, evidence_id="EV-OLD",
                   valid_from="2025-01-01", valid_to="2025-12-31"),
            record("R-EXPIRED", "P1", quantity=5, factor_id="F-OLD",
                   occurred_on="2026-03-01"),
            record("R-UNKNOWN", "P1", quantity=5, factor_id="F-NEVER",
                   occurred_on="2026-03-01"),
        ]
        qmap = quarantine_map(build(events).result())
        self.assertEqual(qmap["R-EXPIRED"], ["FACTOR_EXPIRED"])
        self.assertEqual(qmap["R-UNKNOWN"], ["FACTOR_NOT_FOUND"])

    def test_not_yet_valid(self):
        events = [
            evidence("EV-FUT"),
            factor("F-FUT", 1, value=1.0, evidence_id="EV-FUT",
                   valid_from="2027-01-01"),
            record("R-EARLY", "P1", quantity=5, factor_id="F-FUT",
                   occurred_on="2026-03-01"),
        ]
        self.assertEqual(
            quarantine_map(build(events).result())["R-EARLY"],
            ["FACTOR_NOT_YET_VALID"],
        )

    def test_withdrawn_factor_is_quarantined_and_keeps_other_reasons(self):
        events = self.BASE + [
            withdrawal("F1", 1, ts="2026-02-01T00:00:00+00:00"),
            record("R1", "P1", quantity=10, occurred_on="2026-03-01"),
        ]
        self.assertEqual(
            quarantine_map(build(events).result())["R1"], ["FACTOR_WITHDRAWN"]
        )

    def test_geography_scope_exact_and_global(self):
        events = [
            evidence("EV-CN"), evidence("EV-GL"),
            factor("F-CN", 1, value=1.0, evidence_id="EV-CN", geography="CN"),
            factor("F-GL", 1, value=2.0, evidence_id="EV-GL", geography="GLOBAL"),
            record("R-CN", "P1", quantity=10, factor_id="F-CN", geography="CN"),
            record("R-EU", "P1", quantity=10, factor_id="F-CN", geography="EU"),
            record("R-GL", "P1", quantity=10, factor_id="F-GL", geography="EU"),
        ]
        result = build(events).result()
        qmap = quarantine_map(result)
        self.assertNotIn("R-CN", qmap)
        self.assertEqual(qmap["R-EU"], ["GEOGRAPHY_OUT_OF_SCOPE"])
        self.assertNotIn("R-GL", qmap)  # GLOBAL factor applies everywhere

    def test_boundary_mismatch_includes_flow_identity(self):
        events = [
            evidence("EV-B"),
            factor("F-B", 1, value=1.0, evidence_id="EV-B",
                   boundary="CRADLE_TO_GATE"),
            record("R-OK", "P1", quantity=1, factor_id="F-B",
                   boundary="CRADLE_TO_GATE"),
            record("R-BAD", "P1", quantity=1, factor_id="F-B",
                   boundary="GATE_TO_GATE"),
        ]
        qmap = quarantine_map(build(events).result())
        self.assertNotIn("R-OK", qmap)
        self.assertEqual(qmap["R-BAD"], ["BOUNDARY_MISMATCH"])

    def test_unit_incompatible_does_not_silently_become_zero(self):
        events = [
            evidence("EV-T"),
            factor("F-T", 1, value=0.05, unit="kgCO2e/tkm", evidence_id="EV-T",
                   activity="road_freight", material="freight",
                   boundary="TRANSPORT"),
            record("R-F", "P1", quantity=200, unit="tkm", factor_id="F-T",
                   activity="road_freight", material="freight",
                   boundary="TRANSPORT"),
            record("R-X", "P1", quantity=50, unit="kWh", factor_id="F-T",
                   activity="road_freight", material="freight",
                   boundary="TRANSPORT"),
        ]
        result = build(events).result()
        qmap = quarantine_map(result)
        self.assertEqual(qmap["R-X"], ["UNIT_INCOMPATIBLE"])
        self.assertNotIn("R-F", qmap)

    def test_reasons_accumulate_without_short_circuit(self):
        # A withdrawn, expired, out-of-scope factor with boundary mismatch:
        # all applicable reasons must be reported together.
        events = [
            evidence("EV-MANY"),
            factor("F-MANY", 1, value=1.0, evidence_id="EV-MANY",
                   geography="CN", boundary="CRADLE_TO_GATE",
                   valid_from="2025-01-01", valid_to="2025-12-31"),
            withdrawal("F-MANY", 1),
            record("R-MANY", "P1", quantity=3, factor_id="F-MANY",
                   geography="US", boundary="ENERGY",
                   occurred_on="2026-05-01"),
        ]
        reasons = quarantine_map(build(events).result())["R-MANY"]
        self.assertEqual(
            set(reasons),
            {"FACTOR_WITHDRAWN", "FACTOR_EXPIRED", "GEOGRAPHY_OUT_OF_SCOPE",
             "BOUNDARY_MISMATCH"},
        )

    def test_pinned_version_is_judged_on_its_own_merits(self):
        events = [
            evidence("EV-P"),
            factor("F-P", 1, value=9.0, evidence_id="EV-P",
                   valid_from="2025-01-01", valid_to="2025-12-31"),
            factor("F-P", 2, value=1.0, evidence_id="EV-P",
                   valid_from="2026-01-01"),
            # Pinning the old v1 in 2026 -> expired, even though v2 exists.
            record("R-PIN", "P1", quantity=10, factor_id="F-P",
                   factor_version=1, occurred_on="2026-03-01"),
        ]
        self.assertEqual(
            quarantine_map(build(events).result())["R-PIN"], ["FACTOR_EXPIRED"]
        )


if __name__ == "__main__":
    unittest.main()
