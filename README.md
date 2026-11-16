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
