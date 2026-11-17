"""Independent quality assurance layer for carbon-factor-ledger.

This package never modifies the production package under ``src/`` and never
re-implements production accounting logic for delivery.  It provides:

* high precision Decimal reference oracles (``oracles``)
* deterministic activity generators and edge corpora (``generators``)
* explainable assertion helpers (``checks``)
* differential golden snapshots (``snapshots``)
* mutation testing and fault injection harness (``mutators``)
* property / characterization suites (``test_qa_*``)
"""

import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
_SRC = _ROOT / "src"
for _p in (str(_SRC), str(_ROOT)):
    if _p not in sys.path:
        sys.path.insert(0, _p)
