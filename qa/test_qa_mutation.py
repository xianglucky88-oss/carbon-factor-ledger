"""QA layer 4b/4 — mutation testing and fault injection proof.

For every mutant the suite supplies a *kill predicate* that exercises exactly
the audit property which should detect it.  Predicates return "" when the
mutant is detected (assertion failure, exception, or wrong value) and a
non-empty survival reason otherwise.  The identity-conversion control mutant
must *survive*, proving the fault injection harness itself is honest.
"""

import unittest
from decimal import Decimal

from . import generators as G
from .mutators import MUTANTS, run_mutation_suite, MutantSandbox
from .oracles import decimal_oracle, _dec


def _engine(sandbox):
    return sandbox.module("engine")


def _pipeline(sandbox):
    return sandbox.module("pipeline")


def _parser(sandbox):
    return sandbox.module("parser")


# --- per-mutant kill predicates -------------------------------------------

def kill_validity_default_false(sb, mutant):
    rows = [G.make_row("P", 2, 2.0, factor_id="no-flag", include_valid=False)]
    out = _engine(sb).calculate(rows, {})
    if "no-flag" in out["excluded_factors"] or "P" not in out["footprints"]:
        return ""  # default-valid row wrongly excluded -> killed
    return "missing-valid row was still included"


def kill_validity_no_exclusion(sb, mutant):
    rows = [G.make_row("P", 2, 2.0, factor_id="bad", valid=False)]
    out = _engine(sb).calculate(rows, {})
    if "bad" not in out["excluded_factors"]:
        return ""
    return "invalid factor still isolated"


def kill_round_end_only(sb, mutant):
    rows = [G.make_row("P", 0.0007, 1.0, factor_id="r%d" % i) for i in range(3)]
    out = _engine(sb).calculate(rows, {})
    if out["footprints"].get("P") != 0.003:
        return ""  # end-only rounding yields 0.002
    return "incremental rounding not disrupted"


def kill_round_before_multiply(sb, mutant):
    rows = [G.make_row("P", 1.0004, 2.5, factor_id="pre")]
    out = _engine(sb).calculate(rows, {})
    if out["footprints"].get("P") != 2.501:
        return ""  # pre-rounding gives 2.5
    return "pre-multiplication rounding not detected"


def kill_grouping_by_product_unit(sb, mutant):
    rows = [
        G.make_row("P", 2, 2.0, factor_id="a", unit="kg"),
        G.make_row("P", 3, 2.0, factor_id="b", unit="t"),
    ]
    try:
        out = _engine(sb).calculate(rows, {})
    except Exception:
        return ""  # tuple key breaks the dict-key contract downstream
    if set(out["footprints"]) != {"P"}:
        return ""
    return "grouping key change not detected"


def kill_sign_flip(sb, mutant):
    rows = [G.make_row("P", 10, 2.0, factor_id="s")]
    out = _engine(sb).calculate(rows, {})
    if out["footprints"].get("P") != 20.0:
        return ""
    return "sign flip not detected"


def kill_unit_direction_wrong(sb, mutant):
    # Same physical activity expressed in g vs kg; identity-contract says the
    # numeric ledger must not move. The injected 1000x inversion must.
    kg = [G.make_row("P", 1, 2.0, factor_id="kg-row", unit="kg")]
    g = [G.make_row("P", 1, 2.0, factor_id="g-row", unit="g")]
    a = _engine(sb).calculate(kg, {})["footprints"]["P"]
    b = _engine(sb).calculate(g, {})["footprints"]["P"]
    if a != b:
        return ""
    return "inverted unit direction produced identical footprints"


def kill_unit_overconvert(sb, mutant):
    g = [G.make_row("P", 1, 2.0, factor_id="g-row", unit="g")]
    out = _engine(sb).calculate(g, {})
    if out["footprints"]["P"] != 2.0:
        return ""
    return "1,000,000x conversion magnitude not detected"


def control_unit_identity_survives(sb, mutant):
    # Inverse predicate: identity conversion MUST behave like the real ledger.
    from carbon_factor_ledger.engine import calculate as real_calculate
    rows = G.edge_corpus()
    out = _engine(sb).calculate([dict(r) for r in rows], {})
    real = real_calculate([dict(r) for r in rows], {})
    if out == real:
        return "identity control behaved identically (expected survival)"
    return ""  # if identity injection changes behavior, the harness is broken


def kill_report_status_error(sb, mutant):
    result = _pipeline(sb).run({"activities": G.zero_corpus()})
    if result["status"] != "ok":
        return ""
    return "status field mutation not detected"


def kill_report_no_spread(sb, mutant):
    result = _pipeline(sb).run({"activities": [G.make_row("P", 1, 1.0)]})
    if "footprints" not in result:
        return ""
    return "envelope spread mutation not detected"


def kill_pipeline_project_renamed(sb, mutant):
    result = _pipeline(sb).run({"activities": []})
    if result["project"] != "carbon-factor-ledger":
        return ""
    return "project field mutation not detected"


def kill_parser_inverted_list_check(sb, mutant):
    try:
        _parser(sb).load_records({"activities": []})
    except ValueError:
        return ""
    return "inverted parser type check not detected"


PREDICATES = {
    "validity_default_false": kill_validity_default_false,
    "validity_no_exclusion": kill_validity_no_exclusion,
    "round_end_only": kill_round_end_only,
    "round_before_multiply": kill_round_before_multiply,
    "grouping_by_product_unit": kill_grouping_by_product_unit,
    "sign_flip": kill_sign_flip,
    "unit_direction_wrong": kill_unit_direction_wrong,
    "unit_overconvert": kill_unit_overconvert,
    "unit_identity_control": control_unit_identity_survives,
    "report_status_error": kill_report_status_error,
    "report_no_spread": kill_report_no_spread,
    "pipeline_project_renamed": kill_pipeline_project_renamed,
    "parser_inverted_list_check": kill_parser_inverted_list_check,
}


class TestMutation(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.outcomes = run_mutation_suite(PREDICATES)

    def test_all_mutants_are_killed_except_identity_control(self):
        by_name = {o.name: o for o in self.outcomes}
        for mutant in MUTANTS:
            with self.subTest(mutant=mutant.name, error_class=mutant.error_class):
                outcome = by_name[mutant.name]
                if mutant.name == "unit_identity_control":
                    self.assertFalse(
                        outcome.killed,
                        msg="identity control was killed — fault injection "
                            "harness is broken: %s" % outcome.detail,
                    )
                else:
                    self.assertTrue(
                        outcome.killed,
                        msg="surviving mutant %s (%s): %s"
                            % (mutant.name, mutant.error_class, outcome.detail),
                    )

    def test_every_audit_error_class_has_a_killed_representative(self):
        required_classes = {
            "unit direction", "validity boundary", "rounding timing",
            "grouping key", "report field",
        }
        killed_classes = {o.error_class for o in self.outcomes if o.killed}
        self.assertTrue(required_classes <= killed_classes,
                        msg="missing kill coverage for classes: %s"
                            % sorted(required_classes - killed_classes))

    def test_mutant_copies_never_touch_production_tree(self):
        # Baseline engine must still show the real incremental-rounding value.
        from carbon_factor_ledger.engine import calculate
        rows = [G.make_row("P", 0.0007, 1.0, factor_id="r%d" % i)
                for i in range(3)]
        self.assertEqual(calculate(rows, {})["footprints"]["P"], 0.003)


if __name__ == "__main__":
    unittest.main()
