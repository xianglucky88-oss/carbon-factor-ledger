"""Scope/boundary changes via factor versions and partial-material handling."""
import unittest

from carbon_factor_ledger.pipeline import LedgerSession
from tests.util import (
    evidence, factor, footprint_by_cell, parsed, quarantine_map, record,
)


class TestScopeAndBoundaryEvolution(unittest.TestCase):
    def _base(self):
        return [
            evidence("EV-G"),
            factor("F-G", 1, value=1.0, evidence_id="EV-G", geography="CN",
                   boundary="CRADLE_TO_GATE",
                   valid_from="2026-01-01", valid_to="2026-06-30"),
        ]

    def test_new_version_with_narrower_geography_only_affects_new_window(self):
        s = LedgerSession()
        s.apply(parsed(self._base() + [
            # v1 covers an EU record via... actually v1 is CN-only; record EU
            # under v1 window is out of scope.
            record("R-CN", "P1", quantity=10, factor_id="F-G", geography="CN",
                   occurred_on="2026-03-01"),
            record("R-EU-OLD", "P1", quantity=10, factor_id="F-G",
                   geography="EU", occurred_on="2026-03-01"),
        ]))
        qmap = quarantine_map(s.result())
        self.assertEqual(set(qmap["R-EU-OLD"]), {"GEOGRAPHY_OUT_OF_SCOPE"})
        self.assertNotIn("R-CN", qmap)

        # v2 extends to GLOBAL from July: new EU record now resolves, old one
        # (March) remains quarantined because its date predates v2's window.
        s.apply(parsed([
            factor("F-G", 2, value=1.1, evidence_id="EV-G", geography="GLOBAL",
                   boundary="CRADLE_TO_GATE", valid_from="2026-07-01",
                   supersedes=1, ts="2026-06-15T00:00:00+00:00"),
            record("R-EU-NEW", "P1", quantity=10, factor_id="F-G",
                   geography="EU", occurred_on="2026-08-01"),
        ]))
        qmap = quarantine_map(s.result())
        self.assertIn("R-EU-OLD", qmap)      # historical record unchanged
        self.assertNotIn("R-EU-NEW", qmap)   # new window covers EU
        cells = footprint_by_cell(s.result())
        new = next(c for c in cells[("P1", "2026-Q3")]["contributions"]
                   if c["record_id"] == "R-EU-NEW")
        self.assertEqual(new["factor_version"], 2)
        self.assertEqual(new["emission"], 11.0)

    def test_boundary_change_does_not_retroactively_reclassify_old_records(self):
        s = LedgerSession()
        s.apply(parsed([
            evidence("EV-B"),
            factor("F-B", 1, value=1.0, evidence_id="EV-B",
                   boundary="CRADLE_TO_GATE",
                   valid_from="2026-01-01", valid_to="2026-06-30"),
            # v2 narrows boundary to GATE_TO_GATE from July.
            factor("F-B", 2, value=1.0, evidence_id="EV-B",
                   boundary="GATE_TO_GATE", valid_from="2026-07-01",
                   supersedes=1, ts="2026-06-15T00:00:00+00:00"),
            record("R-OLD", "P1", quantity=5, factor_id="F-B",
                   boundary="CRADLE_TO_GATE", occurred_on="2026-03-01"),
            record("R-NEW", "P1", quantity=5, factor_id="F-B",
                   boundary="CRADLE_TO_GATE", occurred_on="2026-08-01"),
        ]))
        qmap = quarantine_map(s.result())
        self.assertNotIn("R-OLD", qmap)
        # The new record's CRADLE_TO_GATE boundary no longer matches v2.
        self.assertEqual(set(qmap["R-NEW"]), {"BOUNDARY_MISMATCH"})


class TestPartialProduct(unittest.TestCase):
    def test_product_with_some_missing_material_is_incomplete_not_zero(self):
        s = LedgerSession()
        s.apply(parsed([
            evidence("EV-OK"),
            factor("F-OK", 1, value=3.0, evidence_id="EV-OK"),
            record("R-HAVE", "P1", quantity=10, factor_id="F-OK",
                   material="steel", occurred_on="2026-03-01"),
            # Material the supplier never provided a factor for.
            record("R-MISSING", "P1", quantity=4, factor_id="F-NO-SUCH",
                   material="rare_earth", occurred_on="2026-03-02"),
        ]))
        cells = footprint_by_cell(s.result())
        fp = cells[("P1", "2026-Q1")]
        self.assertEqual(fp["status"], "INCOMPLETE")
        self.assertEqual(fp["footprint_kgco2e"], 30.0)     # valid part counted
        self.assertEqual(fp["included_records"], 1)
        self.assertEqual(fp["quarantined_records"], 1)
        self.assertEqual(fp["coverage_ratio"], 0.5)
        q = fp["quarantined"][0]
        self.assertEqual(q["record_id"], "R-MISSING")
        self.assertEqual(set(q["reasons"]), {"FACTOR_NOT_FOUND"})
        self.assertEqual(q["quantity"], 4.0)               # not silently zero


if __name__ == "__main__":
    unittest.main()
