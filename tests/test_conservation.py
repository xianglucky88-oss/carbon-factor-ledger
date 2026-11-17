"""Conservation invariants on the combined end-to-end fixture.

"Footprint and evidence-reference conservation" means:

1. Every input record lands in exactly one place: a paid contribution or a
   quarantined entry — never both, never neither, never silently zero.
2. The cell footprint equals the sum of its contributions to the last digit
   (no hidden rounding bucket).
3. Every contribution cites an evidence id whose stored hash matches the hash
   embedded in the contribution (no dangling evidence references).
4. Quarantined quantities are retained and reported.
5. Frozen report snapshots keep their original values after later events.
"""
import json
import unittest
from pathlib import Path

from carbon_factor_ledger.pipeline import STRATEGIES, run

FIXTURE = Path(__file__).parents[1] / "fixtures" / "input.json"


def load():
    return json.loads(FIXTURE.read_text())


def expected_record_ids(payload):
    return sorted(
        e["payload"]["record_id"]
        for e in payload["events"]
        if e["event_type"] == "consumption_recorded"
    )


class TestConservation(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.payload = load()
        cls.results = {s: run(cls.payload, strategy=s) for s in STRATEGIES}

    def test_all_three_strategies_are_numerically_identical(self):
        ref = self.results["materialized"]
        for strategy in ("graph", "report_time"):
            other = self.results[strategy]
            self.assertEqual(
                [(f["product"], f["period"], f["footprint_kgco2e"], f["status"])
                 for f in ref["product_footprints"]],
                [(f["product"], f["period"], f["footprint_kgco2e"], f["status"])
                 for f in other["product_footprints"]],
            )
            self.assertEqual(
                ref["totals"], other["totals"]
            )
            self.assertEqual(
                ref["ledger_state_hash"], other["ledger_state_hash"]
            )

    def test_every_record_is_accounted_for_exactly_once(self):
        expected = set(expected_record_ids(self.payload))
        for result in self.results.values():
            seen = []
            for fp in result["product_footprints"]:
                seen += [c["record_id"] for c in fp["contributions"]]
                seen += [q["record_id"] for q in fp["quarantined"]]
            self.assertEqual(sorted(seen), sorted(expected))
            self.assertEqual(len(seen), len(set(seen)))  # no duplication

    def test_footprint_equals_sum_of_contributions(self):
        for result in self.results.values():
            for fp in result["product_footprints"]:
                direct = round(sum(c["emission"] for c in fp["contributions"]), 6)
                self.assertEqual(direct, fp["footprint_kgco2e"])
            grand = round(
                sum(f["footprint_kgco2e"] for f in result["product_footprints"]),
                6,
            )
            self.assertEqual(grand, result["totals"]["footprint_kgco2e"])

    def test_evidence_references_are_closed_and_hash_consistent(self):
        result = self.results["materialized"]
        evidence = {e["evidence_id"]: e for e in result["evidence_inventory"]}
        refs = set()
        for fp in result["product_footprints"]:
            for c in fp["contributions"]:
                self.assertIn(c["evidence_id"], evidence)
                # The hash embedded at calculation time matches the ledger's
                # attested hash for that evidence.
                self.assertEqual(c["evidence_sha256"],
                                 evidence[c["evidence_id"]]["sha256"])
                refs.add(c["evidence_id"])
            # evidence_refs is exactly the de-duplicated contribution set
            self.assertEqual(
                set(fp["evidence_refs"]),
                {c["evidence_id"] for c in fp["contributions"]},
            )
        self.assertTrue(refs)

    def test_combined_case_expected_classifications(self):
        qmap = {}
        for fp in self.results["materialized"]["product_footprints"]:
            for q in fp["quarantined"]:
                qmap[q["record_id"]] = q["reasons"]
        self.assertEqual(set(qmap["R1"]),
                         {"FACTOR_WITHDRAWN", "EVIDENCE_HASH_MISMATCH"})
        self.assertEqual(set(qmap["R4"]), {"GEOGRAPHY_OUT_OF_SCOPE"})
        self.assertEqual(set(qmap["R5"]), {"FACTOR_NOT_FOUND"})
        self.assertEqual(set(qmap["R6"]),
                         {"FACTOR_EXPIRED", "BOUNDARY_MISMATCH"})
        self.assertEqual(set(qmap["R7"]), {"EVIDENCE_HASH_MISMATCH"})
        self.assertEqual(set(qmap["R9"]), {"UNIT_INCOMPATIBLE"})
        self.assertEqual(set(qmap["R10"]), {"FACTOR_NOT_FOUND"})
        # R2 (aluminium 500kg x 8), R3 (10 tkm x 0.05) are the Q1 contributors
        cells = {(f["product"], f["period"]): f
                 for f in self.results["materialized"]["product_footprints"]}
        p1q1 = cells[("P1", "2026-Q1")]
        self.assertEqual(p1q1["footprint_kgco2e"], 4000.5)
        self.assertEqual(p1q1["included_records"], 2)
        self.assertEqual(p1q1["quarantined_records"], 4)
        # R8 freight 200 tkm x 0.05 = 10; R11 aluminium 10 kg x 8 = 80
        self.assertEqual(cells[("P2", "2025-Q2")]["footprint_kgco2e"], 10.0)
        self.assertEqual(cells[("P3", "2026-Q1")]["footprint_kgco2e"], 80.0)
        # Partial product: P2/Q1 has no valid factor but keeps both records
        p2q1 = cells[("P2", "2026-Q1")]
        self.assertEqual(p2q1["footprint_kgco2e"], 0.0)
        self.assertEqual(p2q1["status"], "INCOMPLETE")
        self.assertEqual(p2q1["quarantined_records"], 2)

    def test_unit_conversion_in_contribution(self):
        cells = {(f["product"], f["period"]): f
                 for f in self.results["materialized"]["product_footprints"]}
        r3 = next(c for c in cells[("P1", "2026-Q1")]["contributions"]
                  if c["record_id"] == "R3")
        self.assertEqual(r3["quantity"], 10000.0)
        self.assertEqual(r3["unit"], "kgkm")
        self.assertEqual(r3["converted_quantity"], 10.0)
        self.assertEqual(r3["reference_unit"], "tkm")
        self.assertEqual(r3["emission"], 0.5)

    def test_frozen_report_is_not_overwritten(self):
        # The fixture freezes RP1 after the withdrawal/drift events, so the
        # snapshot captures the state at that point in time.
        report = next(
            r for r in self.results["materialized"]["frozen_reports"]
            if r["report_id"] == "RP1"
        )
        self.assertEqual(report["footprint_kgco2e"], 4000.5)
        self.assertEqual(report["status"], "frozen")
        self.assertEqual(len(report["frozen_contributions"]), 2)
        cells = {(f["product"], f["period"]): f
                 for f in self.results["materialized"]["product_footprints"]}
        # Snapshot and live cell are separately preserved objects.
        self.assertEqual(
            report["footprint_kgco2e"],
            cells[("P1", "2026-Q1")]["footprint_kgco2e"],
        )

    def test_freeze_before_withdrawal_preserves_original_value(self):
        # A report frozen *before* a later withdrawal/evidence drift keeps its
        # original footprint forever; only its status changes to superseded.
        from carbon_factor_ledger.pipeline import LedgerSession
        from tests.util import (
            evidence, factor, freeze, parsed, record, withdrawal, h,
        )
        s = LedgerSession()
        s.apply(parsed([
            evidence("EV-S"),
            factor("F-S", 1, value=2.0, evidence_id="EV-S",
                   valid_from="2026-01-01", valid_to="2026-12-31"),
            record("R-A", "P1", quantity=1000, factor_id="F-S",
                   occurred_on="2026-03-01"),
            freeze("RP-EARLY", "P1", "2026-Q1",
                   ts="2026-04-01T00:00:00+00:00"),
        ]))
        frozen = s.ledger.reports["RP-EARLY"]
        self.assertEqual(frozen.footprint, 2000.0)
        self.assertEqual(frozen.status, "frozen")

        s.apply(parsed([
            withdrawal("F-S", 1, ts="2026-09-01T00:00:00+00:00"),
            evidence("EV-S", sha=h("tampered"),
                     ts="2026-09-02T00:00:00+00:00"),
        ]))
        after = s.ledger.reports["RP-EARLY"]
        self.assertEqual(after.footprint, 2000.0)          # value immutable
        self.assertEqual(after.status, "superseded")       # marker updated
        self.assertEqual(len(after.contributions), 1)
        # Live cell is now fully quarantined but the report still holds R-A.
        cells = {(f["product"], f["period"]): f
                 for f in s.result()["product_footprints"]}
        self.assertEqual(cells[("P1", "2026-Q1")]["footprint_kgco2e"], 0.0)
        self.assertEqual(cells[("P1", "2026-Q1")]["quarantined_records"], 1)

    def test_evidence_hash_drift_is_flagged_in_inventory(self):
        inv = {e["evidence_id"]: e
               for e in self.results["materialized"]["evidence_inventory"]}
        steel = inv["EV-STEEL-1"]
        self.assertEqual(steel["integrity"], "CONFLICT")
        self.assertEqual(steel["import_count"], 2)
        self.assertEqual(len(steel["hash_conflicts"]), 1)


if __name__ == "__main__":
    unittest.main()
