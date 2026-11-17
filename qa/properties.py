"""Minimal in-tree property testing framework (no external dependencies).

Features: labelled deterministic cases, seeded random generation, and simple
shrinking that reports the smallest counterexample found.  A failing property
always carries the full activity context via the assertion helpers in
``checks``.
"""

from __future__ import annotations

import copy
from typing import Any, Callable, List, Sequence, Tuple

from .generators import (
    EDGE_FACTORS,
    EDGE_QUANTITIES,
    SAFE_FACTORS,
    SAFE_QUANTITIES,
    make_row,
    random_activities,
    seeded_rng,
)

Property = Callable[[List[dict]], None]


def for_all(property_fn: Property, *, seeds: Sequence[int] = range(1, 41),
            sizes: Sequence[int] = (1, 3, 8, 16, 32),
            safe: bool = False) -> Tuple[bool, dict]:
    """Run ``property_fn`` over deterministic generated corpora.

    Returns ``(passed, counterexample)``.  On failure the counterexample is
    shrunk by removing rows and substituting edge values while the property
    still fails.
    """
    last_error: Any = None
    for seed in seeds:
        for size in sizes:
            rows = random_activities(seed, n=size, safe=safe)
            try:
                property_fn(rows)
            except AssertionError as exc:
                shrunk = _shrink(rows, property_fn)
                return False, {"seed": seed, "size": size,
                               "counterexample": shrunk, "error": str(exc)}
            except Exception as exc:  # pragma: no cover - surfaced to caller
                last_error = exc
                shrunk = _shrink(rows, property_fn, expect_raise=True)
                return False, {"seed": seed, "size": size,
                               "counterexample": shrunk,
                               "error": "%s: %s" % (type(exc).__name__, exc)}
    if last_error is not None:  # pragma: no cover
        raise last_error
    return True, {}


def _fails(rows: List[dict], property_fn: Property, expect_raise: bool = False) -> bool:
    try:
        property_fn(rows)
    except Exception:
        return True
    return False


def _shrink(rows: List[dict], property_fn: Property, *,
            expect_raise: bool = False) -> List[dict]:
    """Greedily remove rows, then shrink each quantity/factor toward zero."""
    current = copy.deepcopy(rows)
    # 1) drop rows while the property still fails
    changed = True
    while changed and len(current) > 1:
        changed = False
        for i in range(len(current)):
            candidate = current[:i] + current[i + 1:]
            if _fails(candidate, property_fn, expect_raise):
                current = candidate
                changed = True
                break
    # 2) shrink scalar fields to smaller edge values
    qs = [0, 1, -1, 2]
    fs = [0.0, 1.0, -1.0, 2.0]
    for i, row in enumerate(current):
        for field, values in (("quantity", qs), ("factor", fs)):
            for small in values:
                candidate = copy.deepcopy(current)
                candidate[i][field] = small
                if _fails(candidate, property_fn, expect_raise):
                    current = candidate
                    break
    return current


def labelled_cases() -> List[dict]:
    """Deterministic labelled smoke set reused across suites."""
    return random_activities(20261117, n=10) + random_activities(424242, n=10, safe=True)
