"""QA layer 4a/4 — property tests and differential golden snapshots."""

import json
import unittest
from decimal import Decimal

from carbon_factor_ledger.engine import calculate

from . import generators as G
from .checks import (
    assert_conservation_split_merge,
    assert_exclusion_and_evidence,
    assert_footprints_match_oracle,
    assert_order_invariance,
)
from .oracles import (
    decimal_oracle,
    exact_sum_oracle,
    float_oracle,
    conservative_rounding_residue,
    _dec,
)
from .properties import for_all
from .snapshots import assert_or_update_snapshot


class TestProperties(unittest.TestCase):
    def test_conservation_under_all_generators(self):
        def prop(rows):
            result = calculate([dict(r) for r in rows], {})
            exact = exact_sum_oracle(rows)
            bounds = conservative_rounding_residue(rows)
            for product, exact_value in exact.items():
                got = _dec(result["footprints"].get(product, 0))
                assert abs(got - exact_value) <= bounds.get(product, Decimal("0")), (
                    product, got, exact_value, bounds.get(product))
        ok, info = for_all(prop, seeds=range(1, 31), sizes=(1, 2, 7, 15, 28))
        self.assertTrue(ok, msg="mass conservation failed: %s" % info)

    def test_safe_generation_is_exactly_invariant(self):
        def prop(rows):
            base = calculate([dict(r) for r in rows], {})
            for seed in range(4):
                other = calculate([dict(r) for r in G.shuffled(rows, seed)], {})
                assert base["footprints"] == other["footprints"]
                assert sorted(base["excluded_factors"]) == sorted(
                    other["excluded_factors"])
        ok, info = for_all(prop, seeds=range(1, 21), sizes=(2, 6, 12), safe=True)
        self.assertTrue(ok, msg="exact invariance failed: %s" % info)

    def test_split_merge_property(self):
        def prop(rows):
            # split every positive integer-quantity included row into 1+rest
            original = [r for r in rows
                        if isinstance(r["quantity"], int) and r["quantity"] >= 2
                        and r.get("valid", True)]
            if not original:
                return
            split_rows = []
            for r in original:
                q = r["quantity"]
                a, b = dict(r), dict(r)
                a["quantity"] = 1
                b["quantity"] = q - 1
                b["factor_id"] = str(r["factor_id"]) + "-b"
                split_rows.extend([a, b])
            assert exact_sum_oracle(original) == exact_sum_oracle(split_rows)
        ok, info = for_all(prop, seeds=range(1, 21), sizes=(4, 10), safe=True)
        self.assertTrue(ok, msg="split/merge property failed: %s" % info)

    def test_zero_quantity_is_identity_element(self):
        def prop(rows):
            base = calculate([dict(r) for r in rows], {})
            with_zeros = [G.make_row(r["product"], 0, r["factor"],
                                     factor_id="zero-%d" % i, unit=r.get("unit"))
                          for i, r in enumerate(rows)] + rows
            extended = calculate([dict(r) for r in with_zeros], {})
            # zero rows may open new 0.0 product keys but never move a value
            for product, value in base["footprints"].items():
                assert extended["footprints"][product] == value
            for product, value in extended["footprints"].items():
                if product not in base["footprints"]:
                    assert value == 0.0
            assert base["excluded_factors"] == extended["excluded_factors"]
        ok, info = for_all(prop, seeds=range(1, 16), sizes=(3, 9))
        self.assertTrue(ok, msg="zero identity failed: %s" % info)

    def test_float_pair_and_decimal_oracle_agree_in_bound(self):
        def prop(rows):
            result = calculate([dict(r) for r in rows], {})
            pair = float_oracle(rows)
            assert result["footprints"] == pair["footprints"]
            assert result["excluded_factors"] == pair["excluded_factors"]
            assert_footprints_match_oracle(self, rows, result)
        ok, info = for_all(prop, seeds=range(1, 21), sizes=(2, 8, 20))
        self.assertTrue(ok, msg="triple-oracle agreement failed: %s" % info)

    def test_exclusion_isolated_under_generation(self):
        from collections import Counter

        def prop(rows):
            result = calculate([dict(r) for r in rows], {})
            oracle = decimal_oracle(rows)
            assert result["excluded_factors"] == oracle.excluded_factors
            # Row-level multiset contract: an id may appear both excluded and
            # in evidence when different activities reuse it; the *counts*
            # must still reconcile exactly with the input activities.
            expected_excluded = Counter(
                r["factor_id"] for r in rows if not r.get("valid", True))
            assert Counter(result["excluded_factors"]) == expected_excluded
            expected_evidence = Counter(
                (r["product"], r["factor_id"])
                for r in rows if r.get("valid", True))
            observed_evidence = Counter(
                (product, fid)
                for product, ids in result["evidence"].items() for fid in ids)
            assert observed_evidence == expected_evidence
        ok, info = for_all(prop, seeds=range(1, 21), sizes=(4, 12))
        self.assertTrue(ok, msg="isolation property failed: %s" % info)


class TestDifferentialSnapshots(unittest.TestCase):
    CASES = {
        "fixture_edge": lambda: G.edge_corpus(),
        "mixed_products": lambda: G.mixed_products_corpus(),
        "valid_boundaries": lambda: G.valid_boundary_corpus(),
        "unit_chain": lambda: G.unit_chain_corpus(),
        "duplicate_factor": lambda: G.duplicate_factor_corpus(),
        "zero_corpus": lambda: G.zero_corpus(),
        "decimal_strings": lambda: G.decimal_strings_corpus(),
        "generated_safe": lambda: G.safe_exact_corpus(),
    }

    def test_all_snapshots_match_golden(self):
        for name, builder in self.CASES.items():
            with self.subTest(snapshot=name):
                assert_or_update_snapshot(self, name, builder())

    def test_snapshot_pins_float_rounding_artifact(self):
        # The golden file documents that binary float and Decimal half-even
        # disagree on 1.1115; neither view is silently "normalized".
        from .snapshots import snapshot_path
        golden = json.loads(snapshot_path("decimal_strings").read_text())
        ledger_battery = golden["ledger"]["footprints"]["battery"]
        decimal_battery = golden["decimal"]["footprints"]["battery"]
        self.assertEqual(ledger_battery, 21.131)
        self.assertEqual(decimal_battery, "21.132")
        # and the exact contribution is recorded in the trace
        trace = [t for t in golden["decimal"]["traces"]
                 if t["factor_id"] == "d3"][0]
        self.assertEqual(trace["contribution"], "1.1115")
        self.assertEqual(trace["running_before"], "20.020")
        self.assertEqual(trace["running_after"], "21.132")


if __name__ == "__main__":
    unittest.main()
