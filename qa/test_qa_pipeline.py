"""QA layer 3/4 — pipeline orchestration and report envelope oracle."""

import json
import unittest
from decimal import Decimal

from carbon_factor_ledger.pipeline import run
from carbon_factor_ledger.engine import calculate
from carbon_factor_ledger.parser import load_records

from . import generators as G
from .checks import (
    assert_exclusion_and_evidence,
    assert_footprints_match_oracle,
    assert_report_envelope,
    assert_total_equals_detail_sum,
)
from .oracles import (
    _dec,
    conservative_rounding_residue,
    decimal_oracle,
    exact_sum_oracle,
)
from .properties import for_all


class TestPipeline(unittest.TestCase):
    def test_end_to_end_fixture(self):
        import pathlib
        fixture = json.loads(
            (pathlib.Path(__file__).resolve().parents[1]
             / "fixtures" / "input.json").read_text())
        result = run(fixture)
        assert_report_envelope(self, result, fixture["activities"])
        self.assertEqual(result["footprints"], {"P1": 20.0})
        self.assertEqual(result["excluded_factors"], ["f2"])
        self.assertEqual(result["evidence"], {"P1": ["f1"]})

    def test_empty_pipeline_is_still_valid_envelope(self):
        result = run({"activities": []})
        assert_report_envelope(self, result, [])
        self.assertEqual(result["footprints"], {})
        self.assertEqual(result["excluded_factors"], [])
        self.assertEqual(result["evidence"], {})

    def test_pipeline_equals_composed_modules(self):
        # orchestration must add no arithmetic of its own
        def prop(rows):
            payload = {"activities": rows}
            composed = {"project": "carbon-factor-ledger", "status": "ok",
                        **calculate(load_records(payload), payload)}
            self.assertEqual(run(payload), composed)
        ok, info = for_all(prop, seeds=range(1, 21), sizes=(1, 6, 14))
        self.assertTrue(ok, msg="pipeline composition drifted: %s" % info)

    def test_pipeline_property_matches_oracle(self):
        def prop(rows):
            result = run({"activities": rows})
            assert_report_envelope(self, result, rows)
            assert_footprints_match_oracle(self, rows, result)
            assert_exclusion_and_evidence(self, rows, result)
            assert_total_equals_detail_sum(self, rows, result)
        ok, info = for_all(prop, seeds=range(1, 26), sizes=(2, 8, 20))
        self.assertTrue(ok, msg="pipeline oracle property failed: %s" % info)

    def test_total_footprint_equals_sum_of_details(self):
        rows = G.mixed_products_corpus()
        result = run({"activities": rows})
        detail_sum = sum((_dec(v) for v in result["footprints"].values()),
                         Decimal("0"))
        # conservation against the exact oracle, using the shared sound bound
        exact = exact_sum_oracle(rows)
        exact_total = sum(exact.values(), Decimal("0"))
        bounds = conservative_rounding_residue(rows)
        self.assertLessEqual(abs(detail_sum - exact_total),
                             sum(bounds.values(), Decimal("0")))

    def test_evidence_explains_every_included_row_once(self):
        rows = G.edge_corpus()
        result = run({"activities": rows})
        included = [r for r in rows if r.get("valid", True)]
        evidence_count = sum(len(v) for v in result["evidence"].values())
        self.assertEqual(evidence_count, len(included))
        # each evidence factor_id traces back to a real included activity
        for product, ids in result["evidence"].items():
            for fid in ids:
                self.assertTrue(
                    any(r["product"] == product and r["factor_id"] == fid
                        and r.get("valid", True) for r in rows),
                    msg="unexplained evidence entry %r/%r" % (product, fid),
                )

    def test_illegal_payloads_surface_as_exceptions_not_envelopes(self):
        for rows in G.illegal_quantity_corpus():
            with self.assertRaises((TypeError, ValueError)):
                run({"activities": rows})


class TestReportEnvelope(unittest.TestCase):
    def test_field_set_is_stable(self):
        result = run({"activities": G.edge_corpus()})
        self.assertEqual(
            sorted(result.keys()),
            sorted(["project", "status", "footprints",
                    "excluded_factors", "evidence"]),
        )

    def test_project_and_status_are_fixed_contract(self):
        for rows in ([], G.zero_corpus(), G.edge_corpus()):
            result = run({"activities": rows})
            self.assertEqual(result["project"], "carbon-factor-ledger")
            self.assertEqual(result["status"], "ok")

    def test_report_is_json_serializable_and_sortable(self):
        result = run({"activities": G.edge_corpus()})
        encoded = json.dumps(result, sort_keys=True)
        self.assertEqual(json.loads(encoded), result)

    def test_all_generated_reports_satisfy_envelope_contract(self):
        def prop(rows):
            result = run({"activities": rows})
            assert_report_envelope(self, result, rows)
        ok, info = for_all(prop, seeds=range(1, 16), sizes=(1, 5, 12, 30))
        self.assertTrue(ok, msg="envelope contract failed: %s" % info)


if __name__ == "__main__":
    unittest.main()
