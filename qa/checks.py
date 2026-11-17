"""Explainable assertion helpers.

Every assertion failure is rendered with the activities involved, the factor
ids, units, the Decimal oracle and the intermediate running values, so an
auditor never sees a bare "7.2 != 7.1".
"""

from __future__ import annotations

import json
import unittest
from decimal import Decimal
from typing import Any, Dict, List, Optional, Sequence

from .oracles import (
    OracleResult,
    decimal_oracle,
    exact_sum_oracle,
    explain_diff,
    conservative_rounding_residue,
    _dec,
    QUANT,
)


def _dump(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str, indent=2)


def explain(rows: Sequence[dict], extra: Optional[dict] = None,
            oracle: Optional[OracleResult] = None) -> str:
    payload = explain_diff(list(rows), oracle)
    if extra:
        payload["extra"] = extra
    return "\n" + _dump(payload)


def assert_explained(testcase: unittest.TestCase, condition: bool,
                     rows: Sequence[dict], message: str,
                     extra: Optional[dict] = None,
                     oracle: Optional[OracleResult] = None) -> None:
    if not condition:
        testcase.fail(message + explain(rows, extra, oracle=oracle))


def assert_footprints_match_oracle(testcase: unittest.TestCase,
                                   rows: Sequence[dict], actual: dict,
                                   *, tolerance: Optional[Dict[str, Decimal]] = None,
                                   strict: bool = False) -> None:
    rows = list(rows)
    oracle = decimal_oracle(rows)
    residue = tolerance or conservative_rounding_residue(rows)
    actual_fp = actual["footprints"]

    assert_explained(
        testcase, set(actual_fp) == set(oracle.footprints), rows,
        "footprint grouping keys differ from oracle",
        {"actual_keys": sorted(actual_fp), "oracle_keys": sorted(oracle.footprints)},
        oracle=oracle,
    )
    for product, expected in oracle.footprints.items():
        got = _dec(actual_fp[product])
        delta = abs(got - expected)
        if strict:
            ok = got == expected
            bound = Decimal("0")
        else:
            bound = residue.get(product, Decimal("0"))
            ok = delta <= bound
        assert_explained(
            testcase, ok, rows,
            "footprint for %r outside oracle bound" % product,
            {"product": product, "actual": str(got), "oracle": str(expected),
             "abs_delta": str(delta), "allowed_residue": str(bound),
             "strict": strict},
            oracle=oracle,
        )


def assert_exclusion_and_evidence(testcase: unittest.TestCase,
                                  rows: Sequence[dict], actual: dict) -> None:
    rows = list(rows)
    oracle = decimal_oracle(rows)
    assert_explained(
        testcase, list(actual["excluded_factors"]) == oracle.excluded_factors,
        rows, "invalid factor isolation list differs",
        {"actual": actual["excluded_factors"], "oracle": oracle.excluded_factors},
        oracle=oracle,
    )
    assert_explained(
        testcase, {k: sorted(v) for k, v in actual["evidence"].items()}
        == {k: sorted(v) for k, v in oracle.evidence.items()},
        rows, "evidence factor_id grouping differs (order-insensitive)",
        {"actual": actual.get("evidence"), "oracle": oracle.evidence},
        oracle=oracle,
    )


def assert_conservation_split_merge(testcase: unittest.TestCase,
                                    original: Sequence[dict],
                                    split_rows: Sequence[dict]) -> None:
    """Merging split activities must conserve the exact-sum oracle per product."""
    exact_original = exact_sum_oracle(list(original))
    exact_split = exact_sum_oracle(list(split_rows))
    assert_explained(
        testcase, exact_original == exact_split, original,
        "split/merge conservation violated in exact arithmetic",
        {"exact_original": {k: str(v) for k, v in exact_original.items()},
         "exact_split": {k: str(v) for k, v in exact_split.items()}},
    )
    # The quantized ledgers may differ only within the rounding residue bound.
    from carbon_factor_ledger.engine import calculate
    a = calculate([dict(r) for r in original], {})
    b = calculate([dict(r) for r in split_rows], {})
    for product, exact in exact_original.items():
        bound = conservative_rounding_residue(list(split_rows)).get(product, Decimal("0"))
        da = abs(_dec(a["footprints"].get(product, 0)) - exact)
        db = abs(_dec(b["footprints"].get(product, 0)) - exact)
        assert_explained(
            testcase, da <= bound and db <= bound, list(split_rows),
            "quantized split/merge result exceeds rounding residue",
            {"product": product, "exact": str(exact),
             "original_ledger": a["footprints"].get(product, 0),
             "split_ledger": b["footprints"].get(product, 0),
             "bound": str(bound)},
        )


def assert_order_invariance(testcase: unittest.TestCase,
                            variants: Sequence[Sequence[dict]], *,
                            exact: bool) -> None:
    """All orderings must give the same answer; exact for safe corpora, else within bound."""
    from carbon_factor_ledger.engine import calculate
    results = [calculate([dict(r) for r in rows], {}) for rows in variants]
    baseline = results[0]
    for k, rows in enumerate(variants[1:], start=1):
        for product in baseline["footprints"]:
            a, b = _dec(baseline["footprints"][product]), _dec(results[k]["footprints"].get(product, 0))
            if exact:
                ok = a == b
                extra = {"mode": "exact", "product": product}
            else:
                bound = conservative_rounding_residue(list(rows)).get(product, Decimal("0"))
                ok = abs(a - b) <= bound
                extra = {"mode": "bounded", "product": product, "bound": str(bound)}
            assert_explained(testcase, ok, rows,
                             "input-order invariance violated for variant %d" % k,
                             {**extra, "baseline": str(a), "variant": str(b)})
        assert_explained(
            testcase,
            sorted(results[k]["excluded_factors"]) == sorted(baseline["excluded_factors"]),
            rows, "excluded factors changed under reordering",
            {"baseline": baseline["excluded_factors"],
             "variant": results[k]["excluded_factors"]},
        )


def assert_total_equals_detail_sum(testcase: unittest.TestCase,
                                   rows: Sequence[dict], actual: dict) -> None:
    detail_sum = sum((_dec(v) for v in actual["footprints"].values()), Decimal("0"))
    reported_total = actual.get("total_footprint")
    if reported_total is not None:
        assert_explained(
            testcase, _dec(reported_total) == detail_sum, rows,
            "reported total_footprint does not equal sum of product details",
            {"reported_total": str(reported_total), "detail_sum": str(detail_sum)},
        )
    # Internal conservation: details vs exact oracle within aggregate residue.
    exact = exact_sum_oracle(list(rows))
    for product, exact_value in exact.items():
        bound = conservative_rounding_residue(list(rows)).get(product, Decimal("0"))
        got = _dec(actual["footprints"].get(product, 0))
        assert_explained(
            testcase, abs(got - exact_value) <= bound, rows,
            "detail total violates exact-sum conservation bound",
            {"product": product, "ledger": str(got), "exact": str(exact_value),
             "bound": str(bound)},
        )


def assert_report_envelope(testcase: unittest.TestCase, result: dict,
                           rows: Sequence[dict]) -> None:
    for field in ("project", "status", "footprints", "excluded_factors", "evidence"):
        assert_explained(testcase, field in result, rows,
                         "report envelope missing field %r" % field,
                         {"result_keys": sorted(result)})
    assert_explained(testcase, result["project"] == "carbon-factor-ledger",
                     rows, "report project field wrong", {"got": result.get("project")})
    assert_explained(testcase, result["status"] == "ok", rows,
                     "report status field wrong", {"got": result.get("status")})
    assert_explained(testcase, isinstance(result["footprints"], dict), rows,
                     "footprints must be a mapping keyed by product")
    assert_explained(testcase, isinstance(result["excluded_factors"], list), rows,
                     "excluded_factors must be a list")
    assert_explained(testcase, isinstance(result["evidence"], dict), rows,
                     "evidence must be a mapping")


def assert_raises_with_context(testcase: unittest.TestCase, rows: Sequence[dict],
                               expected=(TypeError, ValueError)) -> None:
    from carbon_factor_ledger.engine import calculate
    try:
        calculate([dict(r) for r in rows], {})
    except expected as exc:
        return
    except Exception as exc:  # pragma: no cover - defensive, failure must be loud
        testcase.fail("illegal quantity raised unexpected %s: %r%s"
                      % (type(exc).__name__, exc, explain(rows)))
    testcase.fail("illegal quantity was silently accepted" + explain(rows))
