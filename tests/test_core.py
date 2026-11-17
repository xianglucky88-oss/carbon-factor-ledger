"""Domain-level tests for parser, rules, engine, pipeline and CLI.

These tests run entirely against the packaged deterministic fixtures and
in-memory payloads; no network, no third-party packages.
"""
from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC = REPO_ROOT / "src"
FIXTURES = REPO_ROOT / "fixtures"
RULES = FIXTURES / "rules"

sys.path.insert(0, str(SRC))

from carbon_factor_ledger import engine, parser, pipeline, report, rules  # noqa: E402
from carbon_factor_ledger.errors import (  # noqa: E402
    EvidenceError,
    FactorError,
    UnitError,
    ValidationError,
)


def load_fixture(name: str = "input.json") -> dict:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


class RuleLoadingTests(unittest.TestCase):
    def test_factor_registry_versions_and_hashes_are_bound(self):
        registry = rules.load_factor_registry(RULES / "factors.json")
        self.assertTrue(registry.version.startswith("factors-"))
        self.assertEqual(len(registry.sha256), 64)
        self.assertEqual(registry.get("f1").value, 2.0)
        with self.assertRaises(FactorError):
            registry.get("does-not-exist")

    def test_factor_state_matrix(self):
        registry = rules.load_factor_registry(RULES / "factors.json")
        cases = {
            "f1": (True, "ok"),          # active, in window
            "f2": (False, "invalid"),    # status invalid
            "f4": (False, "expired"),    # valid_to 2026-06-30
            "f5": (False, "not_yet_effective"),  # valid_from 2026-11-01
        }
        for factor_id, expected in cases.items():
            self.assertEqual(
                rules.factor_state(registry.get(factor_id), "2026-10-15"),
                expected,
                factor_id,
            )

    def test_factor_state_without_evaluation_date_is_never_ok(self):
        registry = rules.load_factor_registry(RULES / "factors.json")
        usable, reason = rules.factor_state(registry.get("f1"), None)
        self.assertFalse(usable)
        self.assertEqual(reason, "no_evaluation_date")

    def test_unit_conversion_kg_g_tonne(self):
        unit_rules = rules.load_unit_rules(RULES / "unit_rules.json")
        self.assertEqual(unit_rules.base_unit, "kg")
        self.assertEqual(unit_rules.to_base("kg", 10), (10.0, 1.0))
        self.assertEqual(unit_rules.to_base("g", 5000), (5.0, 0.001))
        self.assertEqual(unit_rules.to_base("t", 2), (2000.0, 1000.0))
        with self.assertRaises(UnitError):
            unit_rules.to_base("pound", 1)

    def test_evidence_manifest_rejects_empty_path(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            shutil.copytree(RULES, root / "rules")
            manifest_path = root / "rules" / "evidence_manifest.json"
            manifest = json.loads(manifest_path.read_text())
            manifest["files"][0]["path"] = "  "
            manifest_path.write_text(json.dumps(manifest))
            with self.assertRaises(EvidenceError):
                rules.load_evidence_manifest(manifest_path)

    def test_evidence_manifest_detects_tampering_and_missing_files(self):
        evidence = rules.load_evidence_manifest(RULES / "evidence_manifest.json")
        resolved = evidence.verify(["E-100", "E-200"])
        self.assertEqual(set(resolved), {"E-100", "E-200"})
        with self.assertRaises(EvidenceError):
            evidence.verify(["E-999"])
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            shutil.copytree(RULES, root / "rules")
            (root / "rules" / "evidence" / "e100-meter.txt").write_text("tampered")
            tampered = rules.load_evidence_manifest(root / "rules" / "evidence_manifest.json")
            with self.assertRaises(EvidenceError):
                tampered.verify(["E-100"])


class ParserTests(unittest.TestCase):
    def test_valid_payload_round_trip(self):
        rows, evaluation_date = parser.parse_input(load_fixture())
        self.assertEqual(evaluation_date, "2026-11-17")
        self.assertEqual(len(rows), 5)

    def test_empty_evidence_id_is_rejected(self):
        with self.assertRaises(ValidationError):
            pipeline.run(load_fixture("negative/input_empty_evidence_id.json"))

    def test_missing_or_malformed_evaluation_date(self):
        payload = load_fixture()
        payload.pop("evaluation_date")
        with self.assertRaises(ValidationError):
            parser.parse_input(payload)
        payload["evaluation_date"] = "17/11/2026"
        with self.assertRaises(ValidationError):
            parser.parse_input(payload)

    def test_empty_activities_rejected(self):
        payload = load_fixture()
        payload["activities"] = []
        with self.assertRaises(ValidationError):
            parser.load_records(payload)

    def test_negative_quantity_rejected(self):
        payload = load_fixture()
        payload["activities"][0]["quantity"] = -1
        with self.assertRaises(ValidationError):
            parser.load_records(payload)


class EngineAndPipelineTests(unittest.TestCase):
    def test_main_fixture_totals_and_exclusions(self):
        envelope = pipeline.run(load_fixture())
        self.assertEqual(envelope["status"], "ok")
        self.assertEqual(envelope["project"], "carbon-factor-ledger")
        self.assertEqual(envelope["footprints"], {"P1": 20.0, "P2": 2.5, "P4": 12.0})
        excluded = {item["factor_id"]: item["reason"] for item in envelope["excluded_factors"]}
        self.assertEqual(excluded, {"f2": "invalid", "f4": "expired"})
        warning_products = {item["product"] for item in envelope["warnings"]}
        self.assertEqual(warning_products, {"P3"})
        self.assertTrue(all(item["code"] == "empty_evidence" for item in envelope["warnings"]))

    def test_rule_versions_and_hashes_bound_into_envelope(self):
        envelope = pipeline.run(load_fixture())
        binding = envelope["rule_binding"]
        for name in ("factors", "units", "evidence"):
            self.assertTrue(binding[name]["version"])
            self.assertEqual(len(binding[name]["sha256"]), 64)
        self.assertEqual(len(envelope["evidence_files"]), 3)

    def test_input_hash_three_level_identity(self):
        envelope = pipeline.run(load_fixture())
        identity = envelope["input"]
        self.assertEqual(len(identity["sha256_raw"]), 64)
        self.assertEqual(len(identity["sha256_canonical"]), 64)
        self.assertEqual(len(identity["sha256_normalized"]), 64)
        shuffled = pipeline.run(load_fixture("negative/input_shuffled.json"))
        self.assertEqual(
            identity["sha256_normalized"], shuffled["input"]["sha256_normalized"]
        )
        self.assertNotEqual(identity["sha256_raw"], shuffled["input"]["sha256_raw"])

    def test_shuffled_inputs_produce_identical_business_envelope(self):
        first = pipeline.run(load_fixture())
        second = pipeline.run(load_fixture("negative/input_shuffled.json"))
        for envelope in (first, second):
            envelope.pop("input")
        self.assertEqual(
            json.dumps(first, sort_keys=True), json.dumps(second, sort_keys=True)
        )

    def test_unknown_factor_and_unknown_unit_fail(self):
        payload = load_fixture()
        payload["activities"][0]["factor_id"] = "fX"
        with self.assertRaises(FactorError):
            pipeline.run(payload)
        payload = load_fixture()
        payload["activities"][0]["unit"] = "pound"
        with self.assertRaises(UnitError):
            pipeline.run(payload)

    def test_not_yet_effective_factor_excluded(self):
        envelope = pipeline.run(load_fixture("negative/input_not_yet_effective.json"))
        self.assertEqual(envelope["footprints"], {})
        self.assertEqual(
            envelope["excluded_factors"][0]["reason"], "not_yet_effective"
        )

    def test_engine_requires_rule_objects(self):
        with self.assertRaises(TypeError):
            engine.calculate(parser.load_records(load_fixture()))

    def test_report_envelope_is_stable(self):
        data = {"footprints": {"P1": 1.0}}
        envelope = report.build_result("p", data)
        self.assertEqual(envelope, {"project": "p", "status": "ok", "footprints": {"P1": 1.0}})


class CliTests(unittest.TestCase):
    def _run_cli(self, *argv: str) -> subprocess.CompletedProcess:
        launcher = (
            "import sys; "
            f"sys.path.insert(0, {str(SRC)!r}); "
            "from carbon_factor_ledger.cli import main; "
            f"raise SystemExit(main({list(argv)!r}))"
        )
        return subprocess.run(
            [sys.executable, "-I", "-B", "-c", launcher],
            cwd=str(REPO_ROOT),
            env=os_environ_utc(),
            text=True,
            capture_output=True,
            check=False,
        )

    def test_cli_success_is_deterministic_json(self):
        result = self._run_cli(str(FIXTURES / "input.json"), str(RULES))
        self.assertEqual(result.returncode, 0, result.stderr)
        first = result.stdout
        second = self._run_cli(str(FIXTURES / "input.json"), str(RULES)).stdout
        self.assertEqual(first, second)
        payload = json.loads(first)
        self.assertEqual(payload["status"], "ok")

    def test_cli_error_exit_codes(self):
        cases = [
            (str(FIXTURES / "negative" / "input_empty_evidence_id.json"), 3),
            (str(FIXTURES / "negative" / "input_malformed.json"), 3),
            (str(FIXTURES / "does-not-exist.json"), 3),
        ]
        for input_path, expected in cases:
            with self.subTest(input_path=input_path):
                result = self._run_cli(input_path, str(RULES))
                self.assertEqual(result.returncode, expected)
                error = json.loads(result.stderr.strip().splitlines()[-1])
                self.assertEqual(error["status"], "error")

    def test_cli_output_file_and_timezone_independence(self):
        with tempfile.TemporaryDirectory() as td:
            out_utc = Path(td) / "utc.json"
            out_tokyo = Path(td) / "tokyo.json"
            ok = self._run_cli(
                str(FIXTURES / "input.json"), str(RULES), str(out_utc)
            )
            self.assertEqual(ok.returncode, 0, ok.stderr)
            launcher = (
                "import sys; "
                f"sys.path.insert(0, {str(SRC)!r}); "
                "from carbon_factor_ledger.cli import main; "
                f"raise SystemExit(main({[str(FIXTURES / 'input.json'), str(RULES), str(out_tokyo)]!r}))"
            )
            env = os_environ_utc()
            env["TZ"] = "Asia/Tokyo"
            proc = subprocess.run(
                [sys.executable, "-I", "-B", "-c", launcher],
                cwd=str(REPO_ROOT),
                env=env,
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertEqual(out_utc.read_bytes(), out_tokyo.read_bytes())


def os_environ_utc() -> dict:
    return {
        "PATH": "/usr/bin:/bin",
        "PYTHONHASHSEED": "0",
        "PYTHONDONTWRITEBYTECODE": "1",
        "TZ": "UTC",
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "SOURCE_DATE_EPOCH": "0",
        "HOME": "/tmp",
    }


if __name__ == "__main__":
    unittest.main(verbosity=2)
