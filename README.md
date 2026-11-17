# Carbon Factor Ledger

Calculate auditable product footprints with factor validity and unit conversion.

## Modules

- `src/carbon_factor_ledger/models.py`: immutable result contract.
- `src/carbon_factor_ledger/parser.py`: input validation and record extraction.
- `src/carbon_factor_ledger/engine.py`: domain calculation only.
- `src/carbon_factor_ledger/report.py`: stable output envelope.
- `src/carbon_factor_ledger/pipeline.py`: orchestration boundary.
- `src/carbon_factor_ledger/cli.py`: deterministic JSON CLI.

## Run

```bash
PYTHONPATH=src python -m carbon_factor_ledger.cli
PYTHONPATH=src python -m unittest discover -s tests -v
```

The fixture is local and deterministic; no network or credentials are required.

## Independent quality assurance layer (`qa/`)

A separate QA package audits the ledger without modifying or re-implementing
production accounting logic. It contains high precision Decimal reference
oracles (`qa/oracles.py`), deterministic edge corpora and a small in-tree
property framework with shrinking (`qa/generators.py`, `qa/properties.py`),
explainable assertions that print activities, factor ids, units and every
intermediate value on failure (`qa/checks.py`), differential golden snapshots
(`qa/snapshots.py`, `qa/snapshots/`), and a fault injection / mutation
harness that copies the production package into sandboxes (`qa/mutators.py`).

Coverage: split/merge mass conservation, input-order invariance, zero and
negative quantities, illegal quantity rejection, unit relabel chains,
factor-valid boundary values, invalid-factor isolation, duplicate
`factor_id` reuse, mixed-product grouping, decimal-vs-float rounding
artifacts (e.g. `1.1115`), incremental vs end-only rounding timing, and
total-vs-detail reconciliation. Mutation testing proves the suite catches
unit direction, validity comparison, rounding timing, grouping key and
report field defects; an identity-conversion control mutant is expected to
survive, proving the harness is honest.

```bash
python run_qa.py
python -m unittest discover -s qa -t .
QA_REGEN_SNAPSHOTS=1 python run_qa.py   # deliberate golden snapshot refresh
```

