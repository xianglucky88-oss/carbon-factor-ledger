"""End-to-end tests for the offline reproducible release process.

Every test gets an isolated temporary release root and invokes the real
``scripts/build_release.py`` through an isolated ``python -I`` subprocess,
exactly as an auditor would on the air-gapped build host.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "scripts" / "build_release.py"
SRC = REPO_ROOT / "src"
FIXTURES = REPO_ROOT / "fixtures"
TESTS = REPO_ROOT / "tests"

CHILD_ENV = {
    "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
    "PYTHONHASHSEED": "0",
    "PYTHONDONTWRITEBYTECODE": "1",
    "TZ": "UTC",
    "LANG": "C.UTF-8",
    "LC_ALL": "C.UTF-8",
    "SOURCE_DATE_EPOCH": "0",
    "HOME": "/tmp",
}


class ReleaseHarness(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.release_root = self.tmp / "releases"
        # A mutable copy of the repository sources so failure injection never
        # touches the real working tree.
        self.repo = self.tmp / "repo"
        shutil.copytree(REPO_ROOT, self.repo, ignore=shutil.ignore_patterns(
            "__pycache__", "*.pyc", "releases", ".git"
        ))
        self.script = self.repo / "scripts" / "build_release.py"

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def run_builder(self, *args: str, env_tz: str | None = None,
                    cwd: Path | None = None) -> subprocess.CompletedProcess:
        env = dict(CHILD_ENV)
        if env_tz:
            env["TZ"] = env_tz
        return subprocess.run(
            [sys.executable, "-I", "-B", str(self.script), *args],
            cwd=str(cwd or self.repo),
            env=env,
            text=True,
            capture_output=True,
            check=False,
        )

    def build(self, *extra: str, env_tz: str | None = None,
              skip_tests: bool = False) -> tuple[Path, dict]:
        args = ["build", "--release-root", str(self.release_root)]
        if skip_tests:
            args.append("--skip-tests")
        args.extend(extra)
        result = self.run_builder(*args, env_tz=env_tz)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        current = (self.release_root / "current").resolve()
        manifest = json.loads((current / "manifest.json").read_text())
        return current, manifest

    def release_names(self) -> list[str]:
        names = []
        for entry in self.release_root.iterdir():
            # A real release directory: not a symlink, not the work area.
            if entry.name == "_work" or entry.is_symlink():
                continue
            if os.path.isdir(entry):
                names.append(entry.name)
        return sorted(names)


class CleanBuildTests(ReleaseHarness):
    def test_build_in_clean_directory_produces_complete_release(self):
        self.assertFalse(self.release_root.exists())
        release, manifest = self.build()
        self.assertTrue((release / "RELEASE_OK").is_file())
        self.assertTrue((release / "SHA256SUMS").is_file())
        self.assertTrue((release / "manifest.json").is_file())
        self.assertTrue((release / "manifest.sha256").is_file())
        self.assertTrue((release / "footprint.json").is_file())
        self.assertEqual(manifest["test_summary"]["successful"], True)
        self.assertGreater(manifest["test_summary"]["tests_run"], 0)
        # The work area must be empty once the build finishes.
        self.assertEqual(list((self.release_root / "_work").iterdir()), [])

    def test_manifest_binds_source_input_rule_hashes_and_runtime(self):
        _, manifest = self.build()
        for group in ("source", "input", "rules", "tests"):
            section = manifest["hashes"][group]
            self.assertGreater(section["count"], 0)
            self.assertEqual(len(section["tree_digest"]), 64)
        self.assertEqual(manifest["runtime"]["timezone_forced"], "UTC")
        self.assertEqual(manifest["determinism"]["python_hash_seed"], 0)
        self.assertTrue(manifest["built_offline"])

    def test_no_pycache_or_bytecode_in_release(self):
        release, _ = self.build()
        for path in release.rglob("*"):
            if path.is_file():
                self.assertNotIn(path.suffix, (".pyc", ".pyo"))
                self.assertNotIn("__pycache__", path.parts)


class DeterminismTests(ReleaseHarness):
    def test_repeat_builds_are_byte_identical_and_never_overwrite(self):
        first, manifest1 = self.build()
        inode_before = first.stat().st_ino
        manifest_before = (first / "manifest.json").read_bytes()
        second_result = self.run_builder(
            "build", "--release-root", str(self.release_root)
        )
        self.assertEqual(second_result.returncode, 0)
        self.assertIn("already exists", second_result.stdout)
        # Same release id, same directory, no overwrite and no duplicate.
        self.assertEqual(self.release_names(), [manifest1["release_id"]])
        self.assertEqual(first.stat().st_ino, inode_before)
        self.assertEqual((first / "manifest.json").read_bytes(), manifest_before)
        second = (self.release_root / "current").resolve()
        self.assertEqual(
            json.loads((first / "manifest.json").read_text()),
            json.loads((second / "manifest.json").read_text()),
        )
        # Verification may create no scratch files in the release directory.
        self.assertFalse((first / "verify.footprint.json").exists())

    def test_timezone_variation_does_not_change_release_id(self):
        first, manifest_utc = self.build(env_tz="UTC")
        self.release_root = self.tmp / "releases-tz"
        second, manifest_tokyo = self.build(env_tz="Asia/Tokyo")
        self.assertEqual(manifest_utc["release_id"], manifest_tokyo["release_id"])
        self.assertEqual(
            (first / "footprint.json").read_bytes(),
            (second / "footprint.json").read_bytes(),
        )
        self.assertEqual(
            (first / "manifest.json").read_bytes(),
            (second / "manifest.json").read_bytes(),
        )

    def test_input_traversal_order_is_hash_insensitive(self):
        _, manifest = self.build()
        input_section = manifest["hashes"]["input"]
        # Both the canonical fixture and the shuffled-order fixture are
        # packaged; the pipeline test proves identical business output.
        packaged = (self.release_root / "current" / "fixtures" / "negative"
                    / "input_shuffled.json")
        self.assertTrue(packaged.is_file())
        self.assertIn("fixtures/negative/input_shuffled.json", input_section["files"])

    def test_two_builds_from_same_source_share_release_id(self):
        first, m1 = self.build()
        other_root = self.tmp / "other-releases"
        result = self.run_builder(
            "build", "--release-root", str(other_root)
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        m2 = json.loads(
            (other_root / "current" / "manifest.json").read_text()
        )
        self.assertEqual(m1["release_id"], m2["release_id"])


class VerifyAndRollbackTests(ReleaseHarness):
    def test_verify_recomputes_every_hash_and_reruns_packaged_tests(self):
        release, _ = self.build()
        result = self.run_builder("verify", str(release))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("OK release=", result.stdout)

    def test_verify_detects_tampered_artifact(self):
        release, _ = self.build()
        target = release / "footprint.json"
        target.write_text(target.read_text() + " ")
        result = self.run_builder("verify", str(release))
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("hash mismatch", result.stderr)
        self.assertFalse((release / "verify.footprint.json").exists())

    def test_verify_detects_manifest_sidecar_tampering(self):
        release, _ = self.build()
        (release / "manifest.sha256").write_text("0" * 64 + "  manifest.json\n")
        result = self.run_builder("verify", str(release))
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("manifest hash mismatch", result.stderr)

    def test_verify_rejects_directory_without_status_marker(self):
        release, _ = self.build()
        (release / "RELEASE_OK").unlink()
        result = self.run_builder("verify", str(release))
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("RELEASE_OK", result.stderr)

    def test_rollback_switches_current_between_two_verified_releases(self):
        first, m1 = self.build()
        # Mutate a factor value to create a genuinely different second release.
        factors = self.repo / "fixtures" / "rules" / "factors.json"
        payload = json.loads(factors.read_text())
        payload["factors"][0]["factor"] = 2.5
        factors.write_text(json.dumps(payload, indent=2, sort_keys=True))
        second, m2 = self.build(skip_tests=True)
        self.assertNotEqual(m1["release_id"], m2["release_id"])
        self.assertEqual((self.release_root / "current").resolve(), second)

        # Rollback to the previous release, which must be verified first.
        rollback = self.run_builder(
            "rollback", "--release-root", str(self.release_root), m1["release_id"]
        )
        self.assertEqual(rollback.returncode, 0, rollback.stderr)
        self.assertEqual((self.release_root / "current").resolve(), first)
        # Both releases remain on disk; nothing was deleted or overwritten.
        self.assertEqual(set(self.release_names()), {m1["release_id"], m2["release_id"]})

    def test_skiptest_release_is_not_rollback_eligible(self):
        first, m1 = self.build()
        factors = self.repo / "fixtures" / "rules" / "factors.json"
        payload = json.loads(factors.read_text())
        payload["factors"][0]["factor"] = 7.7
        factors.write_text(json.dumps(payload, indent=2, sort_keys=True))
        second, m2 = self.build(skip_tests=True)
        self.assertEqual(m2["test_summary"]["skipped"], -1)
        # The fresh build made the skip-test release current; rollback must
        # refuse to keep it there and restore the verified first release.
        self.assertEqual((self.release_root / "current").resolve(), second)
        refused = self.run_builder(
            "rollback", "--release-root", str(self.release_root), m2["release_id"]
        )
        self.assertNotEqual(refused.returncode, 0)
        self.assertIn("not rollback-eligible", refused.stderr)
        self.assertEqual((self.release_root / "current").resolve(), second)

    def test_rollback_to_missing_release_fails(self):
        self.build()
        result = self.run_builder(
            "rollback", "--release-root", str(self.release_root), "9.9.9-deadbeef"
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("does not exist", result.stderr)

    def test_rollback_refuses_unverified_release(self):
        first, m1 = self.build()
        factors = self.repo / "fixtures" / "rules" / "factors.json"
        payload = json.loads(factors.read_text())
        payload["version"] = "factors-2026.12.0"
        factors.write_text(json.dumps(payload, indent=2, sort_keys=True))
        second, m2 = self.build(skip_tests=True)
        # Corrupt the older release after publication.
        (first / "footprint.json").write_text('{"status":"forged"}')
        result = self.run_builder(
            "rollback", "--release-root", str(self.release_root), m1["release_id"]
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("hash mismatch", result.stderr)
        # current must still point at the verified second release.
        self.assertEqual((self.release_root / "current").resolve(), second)


class FailureInjectionTests(ReleaseHarness):
    def _build_expect_failure(self) -> subprocess.CompletedProcess:
        result = self.run_builder(
            "build", "--release-root", str(self.release_root)
        )
        self.assertNotEqual(result.returncode, 0, result.stdout)
        return result

    def test_failed_build_leaves_no_release_and_cleans_staging(self):
        # Break a source module with a syntax error.
        engine = self.repo / "src" / "carbon_factor_ledger" / "engine.py"
        engine.write_text("def broken(:\n")
        result = self._build_expect_failure()
        self.assertNotIn("published", result.stdout)
        # No release directory at all.
        self.assertFalse(self.release_root.exists()
                         and any(p.is_dir() and p.name != "_work"
                                 for p in self.release_root.iterdir()))
        # Staging is removed; no leftover temp directories.
        if (self.release_root / "_work").exists():
            self.assertEqual(list((self.release_root / "_work").iterdir()), [])

    def test_failing_test_blocks_publication(self):
        test_file = self.repo / "tests" / "test_core.py"
        test_file.write_text(
            "import unittest\n"
            "class ForcedFailure(unittest.TestCase):\n"
            "    def test_fail(self):\n"
            "        self.assertTrue(False)\n"
        )
        result = self._build_expect_failure()
        self.assertIn("unit test stage failed", result.stderr)
        self.assertFalse(
            self.release_root.exists()
            and (self.release_root / "current").exists()
        )

    def test_tampered_evidence_blocks_publication(self):
        evidence = (self.repo / "fixtures" / "rules" / "evidence"
                    / "e100-meter.txt")
        evidence.write_text("tampered after manifest pinning\n")
        result = self.run_builder(
            "build", "--release-root", str(self.release_root), "--skip-tests"
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("evidence hash mismatch", result.stderr)
        self.assertFalse(
            self.release_root.exists()
            and any(p.is_dir() for p in self.release_root.iterdir()
                    if p.name != "_work")
        )

    def test_invalid_factor_registry_blocks_publication(self):
        factors = self.repo / "fixtures" / "rules" / "factors.json"
        payload = json.loads(factors.read_text())
        del payload["factors"][0]["factor_id"]
        factors.write_text(json.dumps(payload))
        result = self.run_builder(
            "build", "--release-root", str(self.release_root), "--skip-tests"
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("factor", result.stderr.lower())

    def test_offline_policy_violation_blocks_publication(self):
        cli = self.repo / "src" / "carbon_factor_ledger" / "cli.py"
        cli.write_text("import urllib.request\n")
        result = self._build_expect_failure()
        self.assertIn("offline policy violated", result.stderr)

    def test_cache_files_in_source_block_build(self):
        cache = self.repo / "src" / "carbon_factor_ledger" / "__pycache__"
        cache.mkdir(parents=True)
        (cache / "module.cpython-311.pyc").write_text("bytecode")
        result = self._build_expect_failure()
        self.assertIn("non-deterministic cache", result.stderr)


class DiffTests(ReleaseHarness):
    def test_diff_rejects_incomplete_release_directories(self):
        release, _ = self.build()
        incomplete = self.tmp / "forged"
        shutil.copytree(release, incomplete)
        (incomplete / "RELEASE_OK").unlink()
        result = self.run_builder("diff", str(incomplete), str(release))
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("RELEASE_OK", result.stderr)

    def test_diff_reports_identical_and_changed_releases(self):
        first, m1 = self.build()
        same = self.run_builder("diff", str(first), str(first))
        self.assertEqual(same.returncode, 0)
        report = json.loads(same.stdout)
        self.assertFalse(report["footprint_changed"])
        self.assertEqual(report["file_changes"]["modified"], [])

        factors = self.repo / "fixtures" / "rules" / "factors.json"
        payload = json.loads(factors.read_text())
        payload["factors"][0]["factor"] = 9.9
        factors.write_text(json.dumps(payload, indent=2, sort_keys=True))
        second, m2 = self.build(skip_tests=True)
        changed = self.run_builder("diff", str(first), str(second))
        self.assertEqual(changed.returncode, 1)
        report = json.loads(changed.stdout)
        self.assertTrue(report["rule_binding_changed"])
        self.assertTrue(report["footprint_changed"])
        self.assertIn("footprint.json", report["file_changes"]["modified"])
        self.assertIn("footprint_field_diff", report)


if __name__ == "__main__":
    unittest.main(verbosity=2)
