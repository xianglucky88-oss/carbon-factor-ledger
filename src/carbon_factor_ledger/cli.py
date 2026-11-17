"""Deterministic JSON command-line interface."""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from .pipeline import STRATEGIES, run

DEFAULT_FIXTURE = str(Path(__file__).parents[2] / "fixtures" / "input.json")


def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Evidence-backed carbon factor ledger: applicability, quarantine "
            "and traceable recomputation."
        )
    )
    parser.add_argument("input", nargs="?", default=DEFAULT_FIXTURE)
    parser.add_argument(
        "--strategy",
        choices=STRATEGIES,
        default="materialized",
        help="recomputation strategy (default: materialized)",
    )
    args = parser.parse_args()
    payload = json.loads(Path(args.input).read_text())
    print(
        json.dumps(
            run(payload, strategy=args.strategy),
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    main()
