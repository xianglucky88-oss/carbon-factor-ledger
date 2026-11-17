"""Mutation testing and fault injection harness.

The harness makes *copies* of the production package inside temporary
directories, applies one source-level change per copy (or injects a unit
conversion fault), imports the copy under a unique alias, and runs kill
predicates against it.  The production tree under ``src/`` is never modified.

Each mutant maps to one of the audit-critical error classes the QA layer
must catch:

* unit direction / conversion magnitude (fault injection)
* validity-period boundary comparison
* rounding timing
* grouping key
* report envelope / pipeline fields
"""

from __future__ import annotations

import importlib
import shutil
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Dict, List, Optional

SRC = Path(__file__).resolve().parents[1] / "src" / "carbon_factor_ledger"


@dataclass(frozen=True)
class Mutant:
    name: str
    error_class: str
    description: str
    transform: Optional[Callable[[str], str]] = None
    inject_engine: Optional[str] = None      # full replacement engine source
    package_patch: Optional[Callable[[Path], None]] = None


def _must_replace(text: str, old: str, new: str) -> str:
    if old not in text:
        raise RuntimeError("mutation anchor not found: %r" % old)
    return text.replace(old, new, 1)


# ---------------------------------------------------------------------------
# engine.py mutants (anchors are exact substrings of the current engine)
# ---------------------------------------------------------------------------

def _m_round_end_only(src: str) -> str:
    # Round only once at the end instead of after every running addition.
    return _must_replace(
        src,
        "value=float(row[\"quantity\"])*float(row[\"factor\"]); "
        "totals[row[\"product\"]]=round(totals.get(row[\"product\"],0)+value,3); "
        "evidence.setdefault(row[\"product\"],[]).append(row[\"factor_id\"])",
        "value=float(row[\"quantity\"])*float(row[\"factor\"]); "
        "totals[row[\"product\"]]=totals.get(row[\"product\"],0)+value; "
        "evidence.setdefault(row[\"product\"],[]).append(row[\"factor_id\"])",
    ).replace(
        'return {"footprints":totals,',
        'return {"footprints":{p:round(v,3) for p,v in totals.items()},',
        1,
    )


def _m_round_before_multiply(src: str) -> str:
    return _must_replace(
        src,
        "value=float(row[\"quantity\"])*float(row[\"factor\"]);",
        "value=round(float(row[\"quantity\"]),3)*round(float(row[\"factor\"]),3);",
    )


def _m_validity_default_true(src: str) -> str:
    # Missing/None validity treated as invalid instead of default-valid.
    return _must_replace(
        src,
        'if not row.get("valid", True): excluded.append(row["factor_id"]); continue',
        'if not row.get("valid", False): excluded.append(row["factor_id"]); continue',
    )


def _m_validity_drop_exclusion(src: str) -> str:
    return _must_replace(
        src,
        'if not row.get("valid", True): excluded.append(row["factor_id"]); continue',
        'if row.get("valid") is False: pass',
    )


def _m_grouping_key_unit(src: str) -> str:
    # Group footprint totals by (product, unit) instead of product.
    return _must_replace(
        src,
        'totals[row["product"]]=round(totals.get(row["product"],0)+value,3); '
        'evidence.setdefault(row["product"],[]).append(row["factor_id"])',
        'key=(row["product"],row.get("unit")); '
        'totals[key]=round(totals.get(key,0)+value,3); '
        'evidence.setdefault(key,[]).append(row["factor_id"])',
    )


def _m_sign_flip(src: str) -> str:
    return _must_replace(
        src,
        "value=float(row[\"quantity\"])*float(row[\"factor\"]);",
        "value=-float(row[\"quantity\"])*float(row[\"factor\"]);",
    )


# ---------------------------------------------------------------------------
# fault injection: a unit conversion layer with a selectable direction
# ---------------------------------------------------------------------------

_INJECTED_ENGINE = '''
SCALE = __SCALE__
def calculate(rows, payload):
    totals={}; excluded=[]; evidence={}
    for row in rows:
        if not row.get("valid", True): excluded.append(row["factor_id"]); continue
        unit_scale = SCALE if row.get("unit") in ("g",) else (1.0/SCALE if row.get("unit") in ("t",) else 1.0)
        value=float(row["quantity"])*float(row["factor"])*unit_scale
        totals[row["product"]]=round(totals.get(row["product"],0)+value,3)
        evidence.setdefault(row["product"],[]).append(row["factor_id"])
    return {"footprints":totals,"excluded_factors":excluded,"evidence":evidence}
'''


def _inject(scale: str) -> str:
    return _INJECTED_ENGINE.replace("__SCALE__", scale)


# ---------------------------------------------------------------------------
# report.py / pipeline.py / parser.py mutants
# ---------------------------------------------------------------------------

def _m_report_status(src: str) -> str:
    return _must_replace(src, '"status": "ok"', '"status": "error"')


def _m_report_drop_spread(src: str) -> str:
    return _must_replace(src, '**result.data', '"data": result.data')


def _m_pipeline_project(src: str) -> str:
    return _must_replace(src, "build_result('carbon-factor-ledger',",
                         "build_result('mutated-ledger',")


def _m_parser_inverted_check(src: str) -> str:
    return _must_replace(
        src,
        'if not isinstance(rows, list):\n        raise ValueError("activities must be a list")',
        'if isinstance(rows, list):\n        raise ValueError("activities must be a list")',
    )


MUTANTS: List[Mutant] = [
    Mutant("validity_default_false", "validity boundary",
           "missing valid key defaults to invalid", _m_validity_default_true),
    Mutant("validity_no_exclusion", "validity boundary",
           "invalid factors are never isolated", _m_validity_drop_exclusion),
    Mutant("round_end_only", "rounding timing",
           "round once at the end instead of incrementally", _m_round_end_only),
    Mutant("round_before_multiply", "rounding timing",
           "round quantity and factor before multiplying", _m_round_before_multiply),
    Mutant("grouping_by_product_unit", "grouping key",
           "totals keyed on (product, unit)", _m_grouping_key_unit),
    Mutant("sign_flip", "quantity sign",
           "negate every contribution", _m_sign_flip),
    Mutant("unit_direction_wrong", "unit direction",
           "injected g->kg / t->kg conversion with inverted magnitude",
           inject_engine=_inject("1000.0")),
    Mutant("unit_overconvert", "unit direction",
           "injected conversion using an off-by-1000x magnitude",
           inject_engine=_inject("1000000.0")),
    Mutant("unit_identity_control", "fault injection control",
           "injected identity conversion (must survive: behavior unchanged)",
           inject_engine=_inject("1.0")),
    Mutant("report_status_error", "report field",
           "report status flipped to error", _m_report_status),
    Mutant("report_no_spread", "report field",
           "calculation fields nested under data instead of envelope spread",
           _m_report_drop_spread),
    Mutant("pipeline_project_renamed", "report field",
           "project envelope field renamed", _m_pipeline_project),
    Mutant("parser_inverted_list_check", "input validation",
           "activities list type check inverted", _m_parser_inverted_check),
]


@dataclass
class MutantOutcome:
    name: str
    error_class: str
    killed: bool
    detail: str


class MutantSandbox:
    """Materialize one mutated package copy and expose its modules."""

    def __init__(self, mutant: Mutant):
        self.mutant = mutant
        self._tmp: Optional[tempfile.TemporaryDirectory] = None
        self.alias = "mutpkg_%s" % mutant.name
        self._loaded: List[str] = []

    def __enter__(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="qa_mut_")
        pkg_root = Path(self._tmp.name) / self.alias
        shutil.copytree(SRC, pkg_root)

        engine_path = pkg_root / "engine.py"
        if self.mutant.inject_engine is not None:
            engine_path.write_text(self.mutant.inject_engine)
        elif self.mutant.transform is not None:
            target = self._target_file(pkg_root)
            target.write_text(self.mutant.transform(target.read_text()))

        sys.path.insert(0, self._tmp.name)
        self._load_alias(pkg_root)
        return self

    def _target_file(self, pkg_root: Path) -> Path:
        mapping = {
            "report_status_error": "report.py",
            "report_no_spread": "report.py",
            "pipeline_project_renamed": "pipeline.py",
            "parser_inverted_list_check": "parser.py",
        }
        return pkg_root / mapping.get(self.mutant.name, "engine.py")

    def _load_alias(self, pkg_root: Path) -> None:
        import importlib.util
        files = ["__init__", "models", "parser", "engine", "report", "pipeline", "cli"]
        for mod in files:
            full = "%s.%s" % (self.alias, mod)
            spec = importlib.util.spec_from_file_location(
                full, pkg_root / ("%s.py" % mod),
                submodule_search_locations=[str(pkg_root)],
            )
            module = importlib.util.module_from_spec(spec)
            sys.modules[full] = module
            self._loaded.append(full)
            spec.loader.exec_module(module)
        # Make the bare alias importable too.
        sys.modules[self.alias] = sys.modules["%s.__init__" % self.alias]
        self._loaded.append(self.alias)

    def module(self, name: str):
        return importlib.import_module("%s.%s" % (self.alias, name))

    def __exit__(self, *exc):
        for name in self._loaded:
            sys.modules.pop(name, None)
        try:
            sys.path.remove(self._tmp.name)
        except ValueError:
            pass
        assert self._tmp is not None
        self._tmp.cleanup()
        return False


def run_mutation_suite(kill_predicates: Dict[str, Callable[[object, Mutant], str]]) -> List[MutantOutcome]:
    """Execute every mutant. A predicate returns "" when the mutant is killed,
    otherwise a human-readable survival reason."""
    outcomes: List[MutantOutcome] = []
    for mutant in MUTANTS:
        predicate = kill_predicates.get(mutant.name)
        if predicate is None:
            predicate = kill_predicates.get("*")
        assert predicate is not None, "no kill predicate for mutant %s" % mutant.name
        with MutantSandbox(mutant) as sandbox:
            try:
                reason = predicate(sandbox, mutant)
            except Exception as exc:  # a crash under test is a kill
                reason = ""
                detail = "killed via raised %s: %s" % (type(exc).__name__, exc)
            else:
                detail = reason or "killed"
        outcomes.append(MutantOutcome(
            name=mutant.name, error_class=mutant.error_class,
            killed=(reason == ""), detail=detail,
        ))
    return outcomes
