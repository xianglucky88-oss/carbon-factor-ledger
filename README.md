# Carbon Factor Ledger

Calculate auditable product footprints with factor validity, versioned rule
bindings, unit conversion and evidence-hash verification — fully offline and
byte-for-byte reproducible.

## Modules

- `src/carbon_factor_ledger/errors.py`: typed error hierarchy and stable codes.
- `src/carbon_factor_ledger/models.py`: immutable result contract.
- `src/carbon_factor_ledger/rules.py`: versioned factor registry, unit rules and
  evidence manifest (hash verification, factor state machine).
- `src/carbon_factor_ledger/parser.py`: input validation and record extraction.
- `src/carbon_factor_ledger/engine.py`: pure deterministic domain calculation.
- `src/carbon_factor_ledger/report.py`: stable output envelope.
- `src/carbon_factor_ledger/pipeline.py`: orchestration and version/hash binding.
- `src/carbon_factor_ledger/cli.py`: deterministic JSON CLI with fixed exit codes.
- `scripts/build_release.py`: offline reproducible release builder
  (`build | verify | rollback | diff`).
- `docs/AUDIT_RUNBOOK.md`: auditor operating manual (staging, cleanup, diff review).

## Deterministic fixtures

All fixtures are local, static JSON/text files; no network or credentials are
needed.

- `fixtures/input.json`: main activities fixture (valid, invalid and expired
  factors; g/t unit conversions; an empty-evidence warning product).
- `fixtures/rules/factors.json`: factor registry with explicit `version`,
  validity windows and statuses.
- `fixtures/rules/unit_rules.json`: base unit and conversion multipliers.
- `fixtures/rules/evidence_manifest.json` + `evidence/*.txt`: evidence files with
  pinned SHA-256 hashes.
- `fixtures/negative/`: malformed, shuffled and empty-evidence inputs.

## Run locally

```bash
# CLI (isolated interpreter; inject src on sys.path because -I ignores PYTHONPATH)
python3 -I -c "import sys; sys.path.insert(0, 'src'); \
from carbon_factor_ledger.cli import main; raise SystemExit(main())"

# Domain tests
python3 -I -c "import sys, unittest; sys.path.insert(0, 'src'); \
suite = unittest.TestLoader().discover('tests', top_level_dir='.'); \
raise SystemExit(0 if unittest.TextTestRunner(verbosity=2).run(suite).wasSuccessful() else 1)"
```

CLI exit codes: `0` success, `2` argparse usage, `3` expected domain failure
(validation / factor / unit / evidence / rules), `4` unexpected internal error.

## Reproducible offline release

```bash
python3 -I scripts/build_release.py build --release-root ./releases
python3 -I scripts/build_release.py verify  ./releases/current
python3 -I scripts/build_release.py rollback --release-root ./releases <release-id>
python3 -I scripts/build_release.py diff <old-release-dir> <new-release-dir>
```

The release id is `project-version` plus a 12-character source-tree hash. The
manifest binds source/input/rule/test hashes, runtime information, the test
summary and the full artifact list. Build or verification failure never produces
a "successful-looking" package: staging is discarded and existing releases are
never overwritten. See `docs/AUDIT_RUNBOOK.md` for the full contract, mandatory
temp-file cleanup and footprint diff review procedure.
