"""High precision reference oracles.

The oracles are independent of the production engine: they parse the same
*contract* (activity rows with product / quantity / unit / factor /
factor_id / valid) using ``decimal.Decimal`` exact arithmetic, and expose
every intermediate value (per-row contribution, running subtotal) so that a
test failure can explain *where* a number came from.

No oracle here is wired into the product.  They only answer the audit
question "what should the answer be if arithmetic were exact?".
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal, ROUND_HALF_EVEN, getcontext
from typing import Any, Dict, List, Optional

# Plenty of guard digits; nothing in this domain needs more, but a QA oracle
# must not itself become a rounding source.
getcontext().prec = 50

QUANT = Decimal("0.001")  # production ledger quantizes to 3 dp
ZERO = Decimal("0")


def _dec(value: Any) -> Decimal:
    """Exact decimal conversion through the textual/decimal representation."""
    if isinstance(value, bool):
        raise ValueError("boolean is not a numeric quantity/factor: %r" % (value,))
    if isinstance(value, Decimal):
        return value
    if isinstance(value, int):
        return Decimal(value)
    if isinstance(value, float):
        # str(float) is the shortest round-trip representation; this keeps
        # the oracle exact relative to what the input literally denotes.
        return Decimal(str(value))
    if isinstance(value, str):
        return Decimal(value.strip())
    raise TypeError("cannot interpret %r as Decimal" % (value,))


def _is_excluded(row: dict) -> bool:
    """Mirror the ledger's validity *contract* independently.

    Contract derived from the documented fixture: missing ``valid`` means
    valid (default True); an explicit falsy value isolates the factor.
    """
    if "valid" not in row:
        return False
    return not row["valid"]


@dataclass(frozen=True)
class RowTrace:
    """Full explanation of one oracle row evaluation."""

    index: int
    product: str
    factor_id: Any
    unit: Any
    quantity_decimal: Decimal
    factor_decimal: Decimal
    contribution_decimal: Decimal          # quantity * factor, unrounded
    running_total_before: Decimal          # quantized subtotal before this row
    running_total_after: Decimal           # quantized subtotal after this row
    excluded: bool
    raw: dict


@dataclass(frozen=True)
class OracleResult:
    footprints: Dict[str, Decimal]
    excluded_factors: List[Any]
    evidence: Dict[str, List[Any]]
    traces: List[RowTrace] = field(default_factory=list)

    def as_jsonable(self) -> dict:
        return {
            "footprints": {k: str(v) for k, v in sorted(self.footprints.items())},
            "excluded_factors": list(self.excluded_factors),
            "evidence": {k: list(v) for k, v in sorted(self.evidence.items())},
            "traces": [
                {
                    "index": t.index,
                    "product": t.product,
                    "factor_id": t.factor_id,
                    "unit": t.unit,
                    "quantity": str(t.quantity_decimal),
                    "factor": str(t.factor_decimal),
                    "contribution": str(t.contribution_decimal),
                    "running_before": str(t.running_total_before),
                    "running_after": str(t.running_total_after),
                    "excluded": t.excluded,
                }
                for t in self.traces
            ],
        }


def decimal_oracle(rows: List[dict]) -> OracleResult:
    """Exact Decimal oracle using incremental 3 dp half-even quantization."""
    totals: Dict[str, Decimal] = {}
    excluded: List[Any] = []
    evidence: Dict[str, List[Any]] = {}
    traces: List[RowTrace] = []

    for index, row in enumerate(rows):
        excluded_row = _is_excluded(row)
        product = row.get("product")
        factor_id = row.get("factor_id")
        unit = row.get("unit")

        if excluded_row:
            excluded.append(factor_id)
            traces.append(
                RowTrace(
                    index=index, product=product, factor_id=factor_id, unit=unit,
                    quantity_decimal=_dec(row["quantity"]),
                    factor_decimal=_dec(row["factor"]),
                    contribution_decimal=ZERO,
                    running_total_before=totals.get(product, ZERO),
                    running_total_after=totals.get(product, ZERO),
                    excluded=True, raw=dict(row),
                )
            )
            continue

        q = _dec(row["quantity"])
        f = _dec(row["factor"])
        contribution = q * f
        before = totals.get(product, ZERO)
        after = (before + contribution).quantize(QUANT, rounding=ROUND_HALF_EVEN)
        totals[product] = after
        evidence.setdefault(product, []).append(factor_id)
        traces.append(
            RowTrace(
                index=index, product=product, factor_id=factor_id, unit=unit,
                quantity_decimal=q, factor_decimal=f,
                contribution_decimal=contribution,
                running_total_before=before, running_total_after=after,
                excluded=False, raw=dict(row),
            )
        )

    return OracleResult(
        footprints={k: totals[k] for k in sorted(totals)},
        excluded_factors=excluded,
        evidence={k: evidence[k] for k in sorted(evidence)},
        traces=traces,
    )


def exact_sum_oracle(rows: List[dict]) -> Dict[str, Decimal]:
    """Conservation oracle: never rounds until the *final* total.

    Split/merge and permutation invariance hold exactly here; the production
    incremental-rounding ledger may only differ by a bounded rounding residue.
    """
    totals: Dict[str, Decimal] = {}
    for row in rows:
        if _is_excluded(row):
            continue
        product = row["product"]
        totals[product] = totals.get(product, ZERO) + _dec(row["quantity"]) * _dec(row["factor"])
    return {k: totals[k] for k in sorted(totals)}


def float_oracle(rows: List[dict]) -> dict:
    """Independent second implementation in plain float (differential pair).

    Written separately from the production engine so that a shared coding
    mistake cannot silently agree.  Same incremental 3 dp rounding contract.
    """
    totals: Dict[str, float] = {}
    excluded: List[Any] = []
    evidence: Dict[str, List[Any]] = {}
    intermediates: List[dict] = []
    for index, row in enumerate(rows):
        if "valid" in row and not row["valid"]:
            excluded.append(row.get("factor_id"))
            intermediates.append({"index": index, "excluded": True,
                                   "running": totals.get(row.get("product"), 0.0)})
            continue
        value = float(row["quantity"]) * float(row["factor"])
        product = row["product"]
        before = totals.get(product, 0.0)
        after = round(before + value, 3)
        totals[product] = after
        evidence.setdefault(product, []).append(row.get("factor_id"))
        intermediates.append({"index": index, "excluded": False,
                              "contribution": value, "running_before": before,
                              "running_after": after})
    return {
        "footprints": {k: totals[k] for k in sorted(totals)},
        "excluded_factors": excluded,
        "evidence": {k: evidence[k] for k in sorted(evidence)},
        "intermediates": intermediates,
    }


def conservative_rounding_residue(rows: List[dict]) -> Dict[str, Decimal]:
    """Sound absolute residue bound between the float ledger and exact truth.

    Two mechanisms contribute per included row:

    1. quantization to 3 dp is at most half a unit (0.0005);
    2. ``round`` is applied to the *binary* representation of a tie value
       (e.g. 0.0025), which may sit on the opposite side of the Decimal
       half-even tie. That flips the chosen direction, so one float step can
       differ from the exact Decimal step by a full unit, 0.001.

    Accumulating n independent steps gives the sound bound n * 0.001.
    The bound is deliberately loose enough never to false-alarm; kill power
    for rounding mutants comes from the crafted exact-value assertions in
    the mutation suite, not from this generic bound.
    """
    per_product: Dict[str, int] = {}
    for row in rows:
        if not _is_excluded(row):
            per_product[row["product"]] = per_product.get(row["product"], 0) + 1
    return {p: Decimal(n) * Decimal("0.001") for p, n in sorted(per_product.items())}


def total_footprint_decimal(result: dict) -> Decimal:
    return sum((_dec(v) for v in result["footprints"].values()), ZERO)


def explain_diff(rows: List[dict], oracle: Optional[OracleResult] = None) -> dict:
    """Compact, serializable failure context: activities, factors, units, intermediates."""
    oracle = oracle or decimal_oracle(rows)
    return {
        "activities": [
            {
                "index": i,
                "product": r.get("product"),
                "factor_id": r.get("factor_id"),
                "unit": r.get("unit"),
                "quantity": r.get("quantity"),
                "factor": r.get("factor"),
                "valid": r.get("valid", True),
            }
            for i, r in enumerate(rows)
        ],
        "oracle": oracle.as_jsonable(),
    }
