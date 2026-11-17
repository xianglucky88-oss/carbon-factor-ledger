"""Pure domain calculation.

The engine never touches the filesystem or environment.  It receives already
validated rows plus the loaded rule objects and returns a plain dict built in
a deterministic order (sorted keys/ids, fixed rounding) so byte-identical
inputs always produce byte-identical output.
"""
from __future__ import annotations

from typing import Any

from .errors import UnitError
from .rules import FactorRegistry, UnitRules, factor_state

ROUND_TOTAL = 3
ROUND_LINE = 6


def calculate(
    rows: list[dict],
    payload: dict | None = None,
    *,
    factors: FactorRegistry | None = None,
    units: UnitRules | None = None,
    evaluation_date: str | None = None,
) -> dict[str, Any]:
    """Aggregate footprints from validated activity rows.

    Positional ``payload`` is accepted for backwards compatibility with the
    original single-argument contract and is otherwise unused.
    """
    if factors is None or units is None:
        raise TypeError("calculate() requires keyword-only 'factors' and 'units' rule objects")

    totals: dict[str, float] = {}
    evidence_by_product: dict[str, list[str]] = {}
    excluded: dict[str, dict[str, str]] = {}
    referenced_evidence: list[str] = []
    products = {str(row["product"]) for row in rows}

    for row in rows:
        product = str(row["product"])
        factor = factors.get(str(row["factor_id"]))
        usable, reason = factor_state(factor, evaluation_date)
        if not usable:
            excluded.setdefault(
                factor.factor_id,
                {"factor_id": factor.factor_id, "reason": reason, "source": factor.source},
            )
            continue

        if factor.unit != units.base_unit:
            raise UnitError(
                f"factor {factor.factor_id!r} is denominated in {factor.unit!r} but the "
                f"unit rule base unit is {units.base_unit!r}"
            )
        quantity_base, multiplier = units.to_base(str(row["unit"]), float(row["quantity"]))
        line = round(quantity_base * factor.value, ROUND_LINE)
        totals[product] = round(totals.get(product, 0.0) + line, ROUND_TOTAL)

        evidence_id = str(row["evidence_id"])
        referenced_evidence.append(evidence_id)
        evidence_by_product.setdefault(product, [])
        if evidence_id not in evidence_by_product[product]:
            evidence_by_product[product].append(evidence_id)

    warnings: list[dict[str, str]] = []
    for product in sorted(products):
        if product not in evidence_by_product:
            # Every eligible row was excluded: there is no evidence chain left.
            warnings.append(
                {
                    "product": product,
                    "code": "empty_evidence",
                    "message": "no usable activity or evidence remains after factor checks",
                }
            )

    return {
        "footprints": {product: totals[product] for product in sorted(totals)},
        "excluded_factors": [excluded[key] for key in sorted(excluded)],
        "evidence": {
            product: sorted(evidence_by_product[product])
            for product in sorted(evidence_by_product)
        },
        "referenced_evidence": sorted(set(referenced_evidence)),
        "warnings": warnings,
    }
