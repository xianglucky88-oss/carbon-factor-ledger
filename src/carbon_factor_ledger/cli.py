"""Deterministic JSON command line interface.

Exit codes are part of the audited contract:

* ``0``  - footprint envelope printed to stdout
* ``3``  - expected domain failure (validation, factor, unit, evidence, rules)
* ``4``  - unexpected internal failure (also surfaced as a JSON error object)
* ``2``  - argparse usage error (standard library convention)

Nothing is ever written to stdout besides the single JSON document; human
messages go to stderr.  Output uses sorted keys and fixed separators so the
bytes can be diffed and hashed directly.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .errors import CarbonLedgerError
from .pipeline import default_rules_dir, run_file


def _emit_error(message: str, code: str) -> int:
    payload = json.dumps(
        {"status": "error", "error_code": code, "message": message},
        sort_keys=True,
        ensure_ascii=False,
    )
    print(payload, file=sys.stderr)
    return 3


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="carbon-factor-ledger",
        description="Calculate auditable product footprints with factor validity "
        "and unit conversion (fully offline, deterministic output).",
    )
    parser.add_argument(
        "input",
        nargs="?",
        default=str(Path(__file__).resolve().parents[2] / "fixtures" / "input.json"),
        help="path to the input activities JSON (defaults to the packaged fixture)",
    )
    parser.add_argument(
        "rules_dir_positional",
        nargs="?",
        default=None,
        metavar="rules_dir",
        help="optional positional rules directory (overrides --rules-dir when given)",
    )
    parser.add_argument(
        "output_positional",
        nargs="?",
        default=None,
        metavar="output",
        help="optional positional output path (overrides --output when given)",
    )
    parser.add_argument(
        "--rules-dir",
        default=None,
        help="directory containing factors.json, unit_rules.json, evidence_manifest.json",
    )
    parser.add_argument(
        "--output",
        default=None,
        help="optional output path; when omitted the envelope is written to stdout",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    rules_dir = (
        args.rules_dir_positional or args.rules_dir or str(default_rules_dir())
    )
    output_path = args.output_positional or args.output
    try:
        envelope = run_file(args.input, rules_dir)
    except CarbonLedgerError as exc:
        return _emit_error(str(exc), exc.code)
    except FileNotFoundError as exc:
        return _emit_error(str(exc), "file_not_found")
    except json.JSONDecodeError as exc:
        return _emit_error(f"input is not valid JSON: {exc}", "invalid_json")
    except Exception as exc:  # pragma: no cover - defensive envelope for audit
        payload = json.dumps(
            {"status": "error", "error_code": "internal_error", "message": repr(exc)},
            sort_keys=True,
            ensure_ascii=False,
        )
        print(payload, file=sys.stderr)
        return 4

    serialized = json.dumps(envelope, sort_keys=True, ensure_ascii=False, indent=2)
    if output_path:
        Path(output_path).write_text(serialized + "\n", encoding="utf-8")
    else:
        print(serialized)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
