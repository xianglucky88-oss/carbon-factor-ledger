"""QA layer 2/4 — factor calculation high-precision oracle tests."""

import unittest
from decimal import Decimal

from carbon_factor_ledger.engine import calculate

from . import generators as G
from .checks import (
    assert_conservation_split_merge,
    assert_exclusion_and_evidence,
    assert_explained,
    assert_footprints_match_oracle,
    assert_order_invariance,
    assert_raises_with_context,
    assert_total_equals_detail_sum,
)
from .oracles import (
    decimal_oracle,
    exact_sum_oracle,
    float_oracle,
    conservative_rounding_residue,
    _dec,
)
from .properties import for_all


class TestFactorOracle(unittest.TestCase):
    def setUp(self):
        self.rows = G.edge_corpus()
        self.actual = calculate([dict(r) for r in self.rows], {})

    def test_matches_decimal_oracle_within_rounding_residue(self):
        assert_footprints_match_oracle(self, self.rows, self.actual)

    def test_matches_independent_float_pair_exactly(self):
        pair = float_oracle(self.rows)
        self.assertEqual(
            self.actual["footprints"], pair["footprints"],
            msg="ledger diverged from independent float pair\n"
                + str(pair["intermediates"]),
        )
        self.assertEqual(self.actual["excluded_factors"], pair["excluded_factors"])

    def test_invalid_factors_are_isolated_and_explained(self):
        assert_exclusion_and_evidence(self, self.rows, self.actual)
        # f2 (valid=false) appears nowhere valid; f5 has one excluded row
        # (valid=None) AND one valid row, so the valid reuse still counts.
        self.assertIn("f2", self.actual["excluded_factors"])
        self.assertIn("f5", self.actual["excluded_factors"])
        used = {fid for ids in self.actual["evidence"].values() for fid in ids}
        self.assertNotIn("f2", used)
        self.assertIn("f5", used)  # the second, valid f5 row is explainable
        # the excluded f5 occurrence itself never generated an evidence entry
        box_evidence = self.actual["evidence"].get("box", [])
        self.assertEqual(box_evidence.count("f5"), 1)

    def test_factor_valid_boundary_semantics(self):
        rows = G.valid_boundary_corpus()
        result = calculate([dict(r) for r in rows], {})
        oracle = decimal_oracle(rows)
        # missing / True / 1 / "yes" included; False / None / 0 / "" excluded
        self.assertEqual(result["excluded_factors"], oracle.excluded_factors)
        self.assertEqual(
            sorted(result["excluded_factors"]),
            sorted(["false", "none", "zero", "empty-str"]),
        )
        # eight rows each 2*2=4; four included -> 16, incrementally quantized
        self.assertEqual(result["footprints"]["P"], 16.0)

    def test_zero_quantities_contribute_nothing(self):
        rows = G.zero_corpus()
        result = calculate([dict(r) for r in rows], {})
        # Contract: included zero-qty rows still open the product key at 0.0
        # but add no mass; the invalid zero row is isolated regardless.
        self.assertEqual(set(result["footprints"]), {"battery"})
        self.assertEqual(result["footprints"]["battery"], 0.0)
        self.assertEqual(result["excluded_factors"], ["z3"])
        # adding zeros to any corpus is an identity on values
        base = calculate([dict(r) for r in G.edge_corpus()], {})
        extended_rows = G.edge_corpus() + rows
        extended = calculate([dict(r) for r in extended_rows], {})
        for product, value in base["footprints"].items():
            self.assertEqual(extended["footprints"][product], value)

    def test_negative_quantities_and_cancellation(self):
        rows = [
            G.make_row("P", 10, 2.0, factor_id="in"),
            G.make_row("P", -4, 2.0, factor_id="out"),
            G.make_row("P", -6, 2.0, factor_id="out2"),
        ]
        result = calculate([dict(r) for r in rows], {})
        # exact conservation: 20 - 8 - 12 == 0
        self.assertEqual(result["footprints"]["P"], 0.0)
        assert_total_equals_detail_sum(self, rows, result)

    def test_illegal_quantities_fail_loudly_with_context(self):
        for rows in G.illegal_quantity_corpus():
            assert_raises_with_context(self, rows)

    def test_duplicate_factor_id_reuse_is_counted_every_time(self):
        rows = G.duplicate_factor_corpus()
        result = calculate([dict(r) for r in rows], {})
        oracle = decimal_oracle(rows)
        # shared appears twice in battery, once in steel, once excluded
        self.assertEqual(result["evidence"]["battery"].count("shared"), 2)
        self.assertEqual(result["evidence"]["steel"].count("shared"), 1)
        self.assertEqual(result["excluded_factors"], ["shared"])
        self.assertEqual(
            _dec(result["footprints"]["battery"]),
            oracle.footprints["battery"],
        )

    def test_mixed_products_keep_independent_group_keys(self):
        rows = G.mixed_products_corpus()
        result = calculate([dict(r) for r in rows], {})
        assert_footprints_match_oracle(self, rows, result)
        self.assertEqual(set(result["footprints"]),
                         {"battery", "steel", "fabric"})
        # battery: (10-3)*2 = 14 (the qty-2 invalid row isolated)
        self.assertEqual(result["footprints"]["battery"], 14.0)
        self.assertEqual(result["footprints"]["steel"], 7.5)
        self.assertEqual(result["footprints"]["fabric"], 2.5)

    def test_decimal_string_inputs_not_float_parse_artifacts(self):
        rows = G.decimal_strings_corpus()
        result = calculate([dict(r) for r in rows], {})
        oracle = decimal_oracle(rows)
        # float production result stays inside the residue bound of exact truth
        assert_footprints_match_oracle(self, rows, result)
        # and the float-boundary row is pinned explicitly:
        # binary float rounds 1.1115 -> 1.111 at 3dp; Decimal half-even -> 1.112
        self.assertEqual(result["footprints"]["battery"],
                         round(0.1 * 0.2 + 1.1115 + 20.0, 3))
        self.assertEqual(oracle.footprints["battery"], Decimal("21.132"))

    def test_rounding_timing_is_incremental_not_end_only(self):
        rows = [G.make_row("P", 0.0007, 1.0, factor_id="r%d" % i)
                for i in range(3)]
        result = calculate([dict(r) for r in rows], {})
        # incremental: 0.001, 0.001, 0.003 ; end-only would give 0.002
        self.assertEqual(result["footprints"]["P"], 0.003)
        exact = exact_sum_oracle(rows)["P"]
        self.assertEqual(exact, Decimal("0.0021"))

    def test_rounding_residue_bound_is_respected(self):
        def prop(rows):
            result = calculate([dict(r) for r in rows], {})
            exact = exact_sum_oracle(rows)
            bounds = conservative_rounding_residue(rows)
            for product, exact_value in exact.items():
                got = _dec(result["footprints"].get(product, 0))
                assert abs(got - exact_value) <= bounds.get(product, Decimal("0"))
        ok, info = for_all(prop, seeds=range(1, 21), sizes=(4, 12, 24))
        self.assertTrue(ok, msg="rounding residue property failed: %s" % info)

    def test_residue_bound_is_calibrated_against_observed_drift(self):
        # Document why the bound is n*0.001 and not n*0.0005: across hundreds
        # of generated corpora we observe single float-vs-Decimal steps of a
        # full 0.001 at binary tie values, while the total drift never
        # exceeds the sound bound.
        worst = Decimal("0")
        for seed in range(1, 61):
            rows = G.random_activities(seed, n=20)
            result = calculate([dict(r) for r in rows], {})
            exact = exact_sum_oracle(rows)
            bounds = conservative_rounding_residue(rows)
            for product, exact_value in exact.items():
                delta = abs(_dec(result["footprints"].get(product, 0))
                            - exact_value)
                worst = max(worst, delta)
                self.assertLessEqual(delta, bounds[product])
        # the 0.001 single-step phenomenon must actually have been observed,
        # otherwise the bound's justification is stale
        self.assertGreaterEqual(worst, Decimal("0.001"),
                                msg="expected to observe at least one full "
                                    "0.001 tie-flip; revisit bound rationale")


class TestSplitMergeAndOrder(unittest.TestCase):
    def test_split_then_merge_conserves_exact_mass(self):
        original, split_rows = G.split_corpus()
        assert_conservation_split_merge(self, original, split_rows)

    def test_safe_corpus_is_strictly_order_invariant(self):
        rows = G.safe_exact_corpus()
        variants = G.all_permutations(rows, cap=12)
        assert_order_invariance(self, variants, exact=True)

    def test_edge_corpus_order_invariant_within_residue(self):
        rows = G.edge_corpus()
        variants = G.all_permutations(rows, cap=12)
        assert_order_invariance(self, variants, exact=False)

    def test_shuffle_property_across_seeds(self):
        def prop(rows):
            base = calculate([dict(r) for r in rows], {})
            for seed in range(5):
                shuffled = G.shuffled(rows, seed)
                other = calculate([dict(r) for r in shuffled], {})
                exact = exact_sum_oracle(rows)
                bounds = conservative_rounding_residue(rows)
                for product in exact:
                    a = _dec(base["footprints"].get(product, 0))
                    b = _dec(other["footprints"].get(product, 0))
                    assert abs(a - b) <= bounds.get(product, Decimal("0"))
                assert sorted(base["excluded_factors"]) == sorted(
                    other["excluded_factors"])
        ok, info = for_all(prop, seeds=range(1, 16), sizes=(3, 8, 16))
        self.assertTrue(ok, msg="shuffle invariance failed: %s" % info)

    def test_unit_relabel_chain_does_not_move_numbers(self):
        # Two-row base in kg vs the same quantities walked through a label
        # chain (kg -> g -> t -> kWh ...): under the unit-invariant contract
        # no number may move; this is the property the unit-direction fault
        # injection (test_qa_mutation) is proven against.
        base = [
            G.make_row("battery", 4, 2.0, factor_id="u1", unit="kg"),
            G.make_row("battery", 6, 1.0, factor_id="u2", unit="kg"),
            G.make_row("steel", 3, 3.0, factor_id="u3", unit="kg"),
        ]
        chain = ["kg", "g", "t", "kWh", "L", "piece"]
        relabelled = G.relabel_units(base, chain)
        a = calculate([dict(r) for r in base], {})
        b = calculate([dict(r) for r in relabelled], {})
        assert_explained(
            self, a["footprints"] == b["footprints"], relabelled,
            "unit conversion chain relabeling changed a numeric footprint",
            {"base": a["footprints"], "relabelled": b["footprints"]},
        )
        # explicit two-row kg/g pair with identical numeric activity
        kg = [G.make_row("P", 1, 2.0, factor_id="x", unit="kg")]
        g = [G.make_row("P", 1, 2.0, factor_id="x", unit="g")]
        self.assertEqual(calculate(kg, {})["footprints"],
                         calculate(g, {})["footprints"])


if __name__ == "__main__":
    unittest.main()
