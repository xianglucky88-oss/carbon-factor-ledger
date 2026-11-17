"""Public-API smoke tests and output envelope contract."""
import json
import unittest
from pathlib import Path

from carbon_factor_ledger import Ledger, LedgerSession, STRATEGIES, run
from tests.util import evidence, factor, parsed, record

FIXTURE = Path(__file__).parents[1] / "fixtures" / "input.json"


class TestPublicApi(unittest.TestCase):
    def test_fixture_runs_under_every_strategy(self):
        payload = json.loads(FIXTURE.read_text())
        for strategy in STRATEGIES:
            result = run(payload, strategy=strategy)
            self.assertEqual(result["status"], "ok")
            self.assertEqual(result["project"], "carbon-factor-ledger")
            self.assertEqual(result["strategy"], strategy)
            self.assertRegex(result["ledger_state_hash"], r"^[0-9a-f]{64}$")
            self.assertTrue(result["product_footprints"])
            self.assertEqual(result["batch"]["rejected"], [])
            self.assertEqual(result["parse_errors"], [])

    def test_envelope_carries_quarantine_and_evidence_sections(self):
        result = run(json.loads(FIXTURE.read_text()))
        self.assertIn("totals", result)
        self.assertIn("quarantine_reasons", result["totals"])
        self.assertTrue(result["totals"]["quarantined_records"] > 0)
        for fp in result["product_footprints"]:
            for key in ("contributions", "quarantined", "evidence_refs",
                        "coverage_ratio", "status"):
                self.assertIn(key, fp)

    def test_ledger_class_is_directly_usable(self):
        ledger = Ledger()
        ledger.apply_batch(parsed([
            evidence("EV-1"),
            factor("F1", 1, value=2.0, evidence_id="EV-1"),
            record("R1", "P1", quantity=10),
        ]))
        self.assertEqual(len(ledger.events), 3)
        self.assertRegex(ledger.state_hash, r"^[0-9a-f]{64}$")
        session = LedgerSession(ledger=ledger)
        session.apply([])
        result = session.result()
        fp = result["product_footprints"][0]
        self.assertEqual(fp["footprint_kgco2e"], 20.0)


if __name__ == "__main__":
    unittest.main()
