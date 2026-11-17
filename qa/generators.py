"""Deterministic activity generators and edge corpora.

No third-party property-testing dependency is required.  A seeded PRNG plus
explicit edge values supplies generation; ``for_all`` in ``properties`` adds
shrinking and labelled cases.
"""

from __future__ import annotations

import copy
import random
from decimal import Decimal
from typing import Any, Dict, Iterable, List, Sequence, Tuple

UNITS = ["kg", "g", "t", "kWh", "L", "piece"]
UNITLESS = "unitless"
PRODUCTS = ["battery", "steel", "fabric", "box", "grid-power"]
FACTOR_IDS = ["f1", "f2", "f3", "f4", "f5", "f6"]

# Values chosen to expose 3 dp rounding timing and float representation.
EDGE_QUANTITIES = [0, 1, 3, 10, 100,
                   0.0007, 0.0005, 0.0004, 0.0015, 1.0004, 1.1115,
                   2.5, 0.3, 7.0001]
EDGE_FACTORS = [0.0, 1.0, 1.5, 2.0, 2.5, 0.001, 3.333, 1.1115]
# Safe values: every quantity*factor product is binary-exact at <=2 dp and
# every partial sum stays <=2 dp, so no 3 dp quantization is ever reached in
# any ordering. These corpora support *strict* order/split invariance.
SAFE_QUANTITIES = [0, 1, 2, 5, 10, 25, 100, 0.5, 1.5, 2.5]
SAFE_FACTORS = [0.0, 1.0, 2.0, 2.5, 0.5, 3.0]


def make_row(product: str, quantity: Any, factor: Any, *,
             factor_id: str = "f1", unit: str = "kg",
             valid: Any = True, include_valid: bool = True) -> dict:
    row: Dict[str, Any] = {
        "product": product,
        "quantity": quantity,
        "unit": unit,
        "factor": factor,
        "factor_id": factor_id,
    }
    if include_valid:
        row["valid"] = valid
    return row


def seeded_rng(seed: int) -> random.Random:
    return random.Random(seed)


def random_activities(seed: int, n: int = 24, *, safe: bool = False) -> List[dict]:
    rng = seeded_rng(seed)
    qs = SAFE_QUANTITIES if safe else EDGE_QUANTITIES
    fs = SAFE_FACTORS if safe else EDGE_FACTORS
    rows: List[dict] = []
    for i in range(n):
        rows.append(
            make_row(
                product=rng.choice(PRODUCTS),
                quantity=rng.choice(qs),
                factor=rng.choice(fs),
                factor_id=rng.choice(FACTOR_IDS),
                unit=rng.choice(UNITS),
                valid=rng.choice([True, True, True, False]),
                include_valid=rng.choice([True, True, False]),
            )
        )
    return rows


def edge_corpus() -> List[dict]:
    """Hand-built boundary corpus covering every requested edge in one place."""
    return [
        make_row("battery", 0, 2.0, factor_id="zero-qty"),                 # zero quantity
        make_row("battery", 10, 2.0, factor_id="f1"),
        make_row("battery", 3, 1.5, factor_id="f2", valid=False),          # isolated invalid factor
        make_row("battery", 0.0007, 1.0, factor_id="round-a"),
        make_row("battery", 0.0007, 1.0, factor_id="round-b"),
        make_row("battery", 0.0007, 1.0, factor_id="round-c"),             # 0.003 vs end-only 0.002
        make_row("battery", 1.0004, 2.5, factor_id="pre-round-killer"),    # 2.501 vs pre-rounded 2.5
        make_row("battery", -4, 1.5, factor_id="negative"),                # negative quantity cancellation
        make_row("battery", 2, 1.5, factor_id="f1"),                       # duplicate factor_id reuse
        make_row("steel", 6, 3.0, factor_id="f3", unit="t"),               # different product / unit
        make_row("steel", -2, 3.0, factor_id="f3"),                        # duplicate id, negative
        make_row("fabric", 1.1115, 1.0, factor_id="float-boundary"),       # float 1.111 vs decimal 1.112
        make_row("fabric", 0, 9.9, factor_id="zero-2"),
        make_row("grid-power", 8, 0.5, factor_id="f4", unit="kWh",
                 include_valid=False),                                     # missing valid -> still valid
        make_row("box", 5, 1.25, factor_id="f5", valid=None),              # explicit None -> falsy -> excluded
        make_row("box", 5, 1.25, factor_id="f5"),
    ]


def valid_boundary_corpus() -> List[dict]:
    """factor-valid boundary rows: missing / True / False / None / 0 / 1 / empty string."""
    return [
        make_row("P", 2, 2.0, factor_id="missing", include_valid=False),   # valid by default
        make_row("P", 2, 2.0, factor_id="true", valid=True),
        make_row("P", 2, 2.0, factor_id="false", valid=False),
        make_row("P", 2, 2.0, factor_id="none", valid=None),
        make_row("P", 2, 2.0, factor_id="zero", valid=0),
        make_row("P", 2, 2.0, factor_id="one", valid=1),
        make_row("P", 2, 2.0, factor_id="empty-str", valid=""),
        make_row("P", 2, 2.0, factor_id="nonempty-str", valid="yes"),
    ]


def illegal_quantity_corpus() -> List[dict]:
    """Quantities the numeric contract cannot accept; the ledger must fail loudly."""
    bad = [
        make_row("P", "not-a-number", 1.0),
        make_row("P", None, 1.0),
        make_row("P", [1], 1.0),
        make_row("P", {"q": 1}, 1.0),
        make_row("P", 1.0, "nope"),
        make_row("P", 1.0, None),
    ]
    out = []
    for row in bad:
        out.append([make_row("P", 3, 2.0, factor_id="good"), row])
    return out


def split_activity(row: dict, parts: Sequence[int]) -> List[dict]:
    """Split one activity's quantity into integer sub-activities (same factor/unit)."""
    total = row["quantity"]
    if not isinstance(total, int) or total < 0 or sum(parts) != total:
        raise ValueError("splits must be non-negative integer parts summing to %r" % total)
    out = []
    for j, part in enumerate(parts):
        child = copy.deepcopy(row)
        child["quantity"] = part
        child["factor_id"] = "%s-split%d" % (row.get("factor_id"), j)
        out.append(child)
    return out


def split_corpus() -> Tuple[List[dict], List[dict]]:
    original = [
        make_row("battery", 10, 2.0, factor_id="bulk"),
        make_row("steel", 10, 1.5, factor_id="f3", valid=True),
        make_row("steel", 2, 1.5, factor_id="f3"),
    ]
    plans = {10: (1, 2, 3, 4), 2: (1, 1)}
    split_rows: List[dict] = []
    for row in original:
        split_rows.extend(split_activity(row, plans[row["quantity"]]))
    return original, split_rows


def shuffled(rows: List[dict], seed: int) -> List[dict]:
    rng = seeded_rng(seed)
    out = copy.deepcopy(rows)
    rng.shuffle(out)
    return out


def all_permutations(rows: List[dict], cap: int = 24) -> List[List[dict]]:
    """Return up to ``cap`` permutations without materializing n! tuples."""
    rng = seeded_rng(777)
    n = len(rows)
    seen = set()
    out: List[List[dict]] = []
    # Always include the identity order.
    identity = tuple(range(n))
    seen.add(identity)
    out.append([copy.deepcopy(rows[i]) for i in identity])
    attempts = 0
    max_attempts = cap * 200
    while len(out) < cap and attempts < max_attempts:
        attempts += 1
        order = tuple(rng.sample(range(n), n))
        if order in seen:
            continue
        seen.add(order)
        out.append([copy.deepcopy(rows[i]) for i in order])
    return out


def relabel_units(rows: List[dict], chain: Sequence[str]) -> List[dict]:
    """Relabel each row's unit by walking a conversion *chain* of labels.

    The current ledger contract is unit-invariant: units are evidence labels,
    not numeric operators.  A chain of relabels therefore must not move any
    number; this generator feeds the unit-direction fault injection test.
    """
    out = copy.deepcopy(rows)
    for i, row in enumerate(out):
        row["unit"] = chain[i % len(chain)]
    return out


def unit_chain_corpus() -> List[dict]:
    chain = ["kg", "g", "t", "kg", "kWh", "L", "piece", "kg"]
    base = [
        make_row("battery", 4, 2.0, factor_id="u1", unit="kg"),
        make_row("battery", 6, 1.0, factor_id="u2", unit="kg"),
        make_row("steel", 3, 3.0, factor_id="u3", unit="kg"),
    ]
    return relabel_units(base, chain)


def mixed_products_corpus() -> List[dict]:
    return [
        make_row(prod, q, f, factor_id="%s-%d" % (prod, i), unit=u, valid=v)
        for i, (prod, q, f, u, v) in enumerate([
            ("battery", 10, 2.0, "kg", True),
            ("steel", 4, 1.5, "t", True),
            ("battery", 2, 2.0, "kg", False),
            ("fabric", 5, 0.5, "m", True),
            ("steel", 1, 1.5, "t", True),
            ("battery", -3, 2.0, "kg", True),
        ])
    ]


def zero_corpus() -> List[dict]:
    return [
        make_row("battery", 0, 2.0, factor_id="z1"),
        make_row("battery", 0, 0.0, factor_id="z2"),
        make_row("steel", 0, 99.0, factor_id="z3", valid=False),
    ]


def duplicate_factor_corpus() -> List[dict]:
    return [
        make_row("battery", 3, 2.0, factor_id="shared"),
        make_row("steel", 5, 2.0, factor_id="shared"),
        make_row("battery", 7, 2.0, factor_id="shared"),
        make_row("fabric", 1, 2.0, factor_id="shared", valid=False),
    ]


def decimal_strings_corpus() -> List[dict]:
    """Decimal-denoted inputs: prove results are not float-parse artifacts."""
    return [
        make_row("battery", "10", "2.0", factor_id="d1"),
        make_row("battery", "0.1", "0.2", factor_id="d2"),       # 0.02 exactly in decimal
        make_row("battery", "1.1115", "1", factor_id="d3"),
        make_row("steel", "3.333", "2.5", factor_id="d4"),
    ]


def safe_exact_corpus(seed: int = 31, n: int = 12) -> List[dict]:
    """Corpus whose every partial sum is exactly 2 dp -> strict equality holds."""
    return random_activities(seed, n=n, safe=True)
