"""Differential golden snapshots.

Each snapshot captures, for one labelled corpus, three independent views:

* ``ledger``      — the production pipeline output
* ``float_pair``  — an independently written float implementation
* ``decimal``     — the exact Decimal oracle with full intermediate traces

Snapshots pin float-specific behavior (e.g. ``1.1115`` rounding to 1.111 in
binary float while half-even Decimal gives 1.112), so any accidental change
to rounding timing or representation is visible as a diff rather than being
absorbed by loose tolerances.

Regenerate deliberately with:  QA_REGEN_SNAPSHOTS=1 python -m unittest ...
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Dict, List

from .oracles import decimal_oracle, float_oracle

SNAPSHOT_DIR = Path(__file__).resolve().parent / "snapshots"


def _canonical(value) -> object:
    if isinstance(value, dict):
        return {str(k): _canonical(v) for k, v in sorted(value.items())}
    if isinstance(value, (list, tuple)):
        return [_canonical(v) for v in value]
    return value


def build_snapshot(name: str, rows: List[dict]) -> Dict[str, object]:
    from carbon_factor_ledger.pipeline import run
    payload = {"activities": [dict(r) for r in rows]}
    ledger = run(payload)
    return _canonical({
        "case": name,
        "n_activities": len(rows),
        "ledger": ledger,
        "float_pair": float_oracle(rows),
        "decimal": decimal_oracle(rows).as_jsonable(),
    })


def snapshot_path(name: str) -> Path:
    return SNAPSHOT_DIR / ("%s.json" % name)


def assert_or_update_snapshot(testcase, name: str, rows: List[dict]) -> None:
    snapshot = build_snapshot(name, rows)
    path = snapshot_path(name)
    if os.environ.get("QA_REGEN_SNAPSHOTS") == "1":
        path.write_text(json.dumps(snapshot, ensure_ascii=False, indent=2,
                                   sort_keys=True) + "\n")
        testcase.skipTest("regenerated snapshot %s" % name)
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(snapshot, ensure_ascii=False, indent=2,
                                   sort_keys=True) + "\n")
        testcase.fail("snapshot %s did not exist; created it for review%s"
                      % (name, _diff_block(snapshot)))
    golden = json.loads(path.read_text())
    testcase.assertEqual(
        golden, snapshot,
        msg="differential snapshot drift for %r\n--- golden vs current ---\n%s\n%s"
            % (name, _side_by_side(golden, snapshot), _activities_block(rows)),
    )


def _diff_block(snapshot: dict) -> str:
    return "\n" + json.dumps(snapshot, ensure_ascii=False, indent=2, sort_keys=True)


def _side_by_side(golden: dict, current: dict) -> str:
    return json.dumps({"golden": golden, "current": current},
                      ensure_ascii=False, indent=2, sort_keys=True)


def _activities_block(rows: List[dict]) -> str:
    return "\nactivities under test:\n" + json.dumps(
        [{"index": i, **r} for i, r in enumerate(rows)],
        ensure_ascii=False, indent=2, sort_keys=True)
