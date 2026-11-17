#!/usr/bin/env python3
"""Offline, reproducible release builder for the carbon factor ledger.

Design contract
---------------
* Pure Python standard library; no network access is attempted anywhere and no
  third-party package is imported (a dedicated test enforces this).
* Every child Python process runs with ``-I`` (isolated: ignores PYTHONPATH,
  user site-packages and environment hooks) and a fixed ``PYTHONHASHSEED`` so
  a planted ``json.py`` or local interpreter customization cannot influence
  the build.
* All work happens in a throw-away staging directory.  A release is only made
  visible through an atomic directory rename *after* an independent
  verification subprocess has recomputed every hash.  Any failure aborts the
  build, removes staging, and never touches an existing release directory.
* The manifest contains no wall-clock timestamps, host names or other
  non-deterministic fields; identical sources always produce an identical
  release id and byte-identical artifacts.

Subcommands: build | verify | rollback | diff
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator, Sequence

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_DIR = REPO_ROOT / "src"
FIXTURES_DIR = REPO_ROOT / "fixtures"
TESTS_DIR = REPO_ROOT / "tests"
PYPROJECT = REPO_ROOT / "pyproject.toml"

PACKAGED_SRC = "src"
PACKAGED_FIXTURES = "fixtures"
PACKAGED_TESTS = "tests"
FOOTPRINT_NAME = "footprint.json"
MANIFEST_NAME = "manifest.json"
MANIFEST_SIDECAR = "manifest.sha256"
CHECKSUMS_NAME = "SHA256SUMS"
STATUS_NAME = "RELEASE_OK"
CURRENT_LINK = "current"

# Fixed, deterministic child-process environment.
CHILD_ENV = {
    "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
    "PYTHONHASHSEED": "0",
    "PYTHONDONTWRITEBYTECODE": "1",
    "TZ": "UTC",
    "LANG": "C.UTF-8",
    "LC_ALL": "C.UTF-8",
    "SOURCE_DATE_EPOCH": "0",
    "HOME": os.environ.get("HOME", "/tmp"),
}

# Extensions that may never appear inside a finished release.
FORBIDDEN_SUFFIXES = {".pyc", ".pyo"}
FORBIDDEN_DIR_NAMES = {"__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache"}


class BuildError(Exception):
    """Raised for every controlled build/verify failure."""


# --------------------------------------------------------------------------- #
# Hashing / file helpers
# --------------------------------------------------------------------------- #
def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def canonical_digest(obj: Any) -> str:
    return sha256_bytes(
        json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode(
            "utf-8"
        )
    )


def iter_input_files(root: Path) -> Iterator[Path]:
    """Yield all files under ``root`` in a deterministic sorted order."""
    for path in sorted(root.rglob("*"), key=lambda p: p.relative_to(root).as_posix()):
        if path.is_file():
            yield path


def ensure_clean_tree(root: Path) -> None:
    """Reject caches/bytecode before packaging; they are non-deterministic."""
    offenders: list[str] = []
    for path in iter_input_files(root):
        rel = path.relative_to(root)
        if path.suffix in FORBIDDEN_SUFFIXES:
            offenders.append(rel.as_posix())
        if any(part in FORBIDDEN_DIR_NAMES for part in rel.parts):
            offenders.append(rel.as_posix())
    if offenders:
        raise BuildError(
            "non-deterministic cache/bytecode files must be removed first: "
            + ", ".join(sorted(set(offenders)))
        )


def prune_caches(root: Path) -> list[str]:
    """Remove cache directories/bytecode under ``root``; return removed paths."""
    removed: list[str] = []
    for cache_dir in sorted(root.rglob("__pycache__"), reverse=True):
        if cache_dir.is_dir():
            shutil.rmtree(cache_dir)
            removed.append(cache_dir.relative_to(root).as_posix())
    for path in iter_input_files(root):
        if path.suffix in FORBIDDEN_SUFFIXES:
            path.unlink()
            removed.append(path.relative_to(root).as_posix())
    for name in (".pytest_cache", ".mypy_cache", ".ruff_cache"):
        for cache in sorted(root.rglob(name), reverse=True):
            if cache.is_dir():
                shutil.rmtree(cache)
                removed.append(cache.relative_to(root).as_posix())
    return sorted(set(removed))


def tree_hashes(root: Path) -> dict[str, str]:
    """Return ``repo-relative posix path -> sha256`` for a clean tree."""
    return {
        path.relative_to(root).as_posix(): sha256_file(path)
        for path in iter_input_files(root)
    }


def hash_group(root: Path, members: Sequence[Path]) -> dict[str, Any]:
    files = {path.relative_to(root).as_posix(): sha256_file(path) for path in members}
    return {
        "algorithm": "sha256",
        "count": len(files),
        "tree_digest": canonical_digest(files),
        "files": dict(sorted(files.items())),
    }


def read_project_version() -> str:
    text = PYPROJECT.read_text(encoding="utf-8")
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("version") and "=" in stripped:
            value = stripped.split("=", 1)[1].strip().strip('"').strip("'")
            if value:
                return value
    raise BuildError("could not read project version from pyproject.toml")


# --------------------------------------------------------------------------- #
# Isolated process execution
# --------------------------------------------------------------------------- #
def isolated_python(
    code: str,
    cwd: Path,
    extra_env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess:
    env = dict(CHILD_ENV)
    if extra_env:
        env.update(extra_env)
    return subprocess.run(
        [sys.executable, "-I", "-B", "-c", code],
        cwd=str(cwd),
        env=env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )


def cli_launcher(src_path: str, argv: Sequence[str]) -> str:
    """Build isolated ``-c`` code that invokes the CLI with explicit argv."""
    return (
        "import sys; "
        f"sys.path.insert(0, {src_path!r}); "
        "from carbon_factor_ledger.cli import main; "
        f"raise SystemExit(main({list(argv)!r}))"
    )


# --------------------------------------------------------------------------- #
# Staging
# --------------------------------------------------------------------------- #
class Staging:
    """Handle to a throw-away staging directory.

    Set ``published = True`` once the directory has been atomically renamed to
    its final release location, so the context manager does not delete it.
    """

    def __init__(self, path: Path) -> None:
        self.path = path
        self.published = False

    def __fspath__(self) -> str:
        return str(self.path)

    def __truediv__(self, other: str) -> Path:
        return self.path / other

    def __str__(self) -> str:
        return str(self.path)

    def rglob(self, pattern: str):
        return self.path.rglob(pattern)

    def is_dir(self) -> bool:
        return self.path.is_dir()

    def resolve(self) -> Path:
        return self.path.resolve()


@contextmanager
def staging_area(work_root: Path, release_id: str) -> Iterator[Staging]:
    work_root.mkdir(parents=True, exist_ok=True)
    path = Path(tempfile.mkdtemp(prefix=f".stage-{release_id}-", dir=str(work_root)))
    stage = Staging(path)
    try:
        yield stage
    finally:
        if not stage.published and stage.path.exists():
            shutil.rmtree(stage.path)


def copy_tree(src: Path, dst: Path) -> None:
    shutil.copytree(src, dst, ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "*.pyo"))


def run_unit_tests(stage: Path) -> dict[str, Any]:
    """Run the unittest suite inside staging with an isolated interpreter.

    The child writes the full human-readable runner log to ``test_output.log``
    and prints a single deterministic JSON summary line on stdout.
    """
    log_path = stage / "test_output.log"
    code = (
        "import sys, json, unittest\n"
        f"sys.path.insert(0, {str(stage / PACKAGED_SRC)!r})\n"
        "loader = unittest.TestLoader()\n"
        f"suite = loader.discover({str(stage / PACKAGED_TESTS)!r}, pattern='test_core.py',\n"
        f"                       top_level_dir={str(stage)!r})\n"
        "def collect(s, acc):\n"
        "    for x in s:\n"
        "        if isinstance(x, unittest.TestSuite):\n"
        "            collect(x, acc)\n"
        "        else:\n"
        "            acc.append(x.id())\n"
        "ids = []\n"
        "collect(suite, ids)\n"
        f"with open({str(log_path)!r}, 'w', encoding='utf-8') as log:\n"
        "    result = unittest.TextTestRunner(stream=log, verbosity=2).run(suite)\n"
        "summary = {'tests_run': result.testsRun,\n"
        "           'failures': len(result.failures),\n"
        "           'errors': len(result.errors),\n"
        "           'skipped': len(getattr(result, 'skipped', [])),\n"
        "           'successful': result.wasSuccessful(),\n"
        "           'test_ids': sorted(ids)}\n"
        "print('@@SUMMARY@@' + json.dumps(summary, sort_keys=True))\n"
    )
    proc = isolated_python(code, cwd=stage)
    marker = "@@SUMMARY@@"
    summary_line = next(
        (line for line in proc.stdout.splitlines() if line.startswith(marker)), None
    )
    if summary_line is None:
        raise BuildError(
            f"test harness did not report a summary (rc={proc.returncode})\n"
            f"STDOUT:\n{proc.stdout}\nSTDERR:\n{proc.stderr}"
        )
    summary = json.loads(summary_line[len(marker):])
    if not log_path.is_file() or log_path.stat().st_size == 0:
        raise BuildError("test stage produced no test_output.log content")
    # Normalize the wall-clock timing line ("Ran N tests in 0.123s") so the
    # packaged log is byte-identical across machines and timezones.
    log_text = log_path.read_text(encoding="utf-8")
    normalized_lines = [
        line if not line.startswith("Ran ") or " tests in " not in line
        else f"Ran {summary['tests_run']} tests in <elided-for-reproducibility>"
        for line in log_text.splitlines()
    ]
    log_path.write_text("\n".join(normalized_lines) + "\n", encoding="utf-8")
    if proc.returncode != 0 or not summary["successful"]:
        raise BuildError(
            "unit test stage failed: "
            f"{summary['failures']} failure(s), {summary['errors']} error(s); "
            f"see {log_path}"
        )
    return summary


def run_import_check(stage: Path) -> dict[str, str]:
    """Compile every packaged module and import the public surface offline."""
    src = stage / PACKAGED_SRC
    modules: list[str] = []
    for path in iter_input_files(src):
        if path.suffix != ".py":
            continue
        # Pure syntax check via the builtin: no .pyc is written anywhere.
        try:
            compile(path.read_text(encoding="utf-8"), str(path), "exec")
        except SyntaxError as exc:
            raise BuildError(f"syntax compile failed for {path}: {exc}") from exc
        modules.append(path.relative_to(src).with_suffix("").as_posix().replace("/", "."))
    code = (
        "import sys, json, importlib\n"
        f"sys.path.insert(0, {str(src)!r})\n"
        f"mods = {sorted(modules)!r}\n"
        "loaded = {}\n"
        "bad = []\n"
        "for name in mods:\n"
        "    try:\n"
        "        module = importlib.import_module(name)\n"
        "        loaded[name] = module.__name__\n"
        "    except Exception as exc:\n"
        "        bad.append([name, repr(exc)])\n"
        "print(json.dumps({'loaded': loaded, 'bad': bad}, sort_keys=True))\n"
    )
    proc = isolated_python(code, cwd=stage)
    if proc.returncode != 0:
        raise BuildError(f"import stage interpreter failed: {proc.stderr.strip()}")
    report = json.loads(proc.stdout.strip().splitlines()[-1])
    if report["bad"]:
        raise BuildError(f"module import failures: {report['bad']}")
    return dict(sorted(report["loaded"].items()))


def run_footprint(stage: Path) -> Path:
    src = stage / PACKAGED_SRC
    input_path = stage / PACKAGED_FIXTURES / "input.json"
    rules_dir = stage / PACKAGED_FIXTURES / "rules"
    output = stage / FOOTPRINT_NAME
    code = cli_launcher(
        str(src), [str(input_path), str(rules_dir), str(output)]
    )
    proc = isolated_python(code, cwd=stage)
    if proc.returncode != 0:
        raise BuildError(
            f"footprint CLI stage failed (rc={proc.returncode}): {proc.stderr.strip()}"
        )
    if not output.is_file():
        raise BuildError("footprint CLI reported success but produced no output file")
    return output


def check_offline_sources(stage: Path) -> None:
    """Static guard: packaged code must not import network-capable modules."""
    forbidden = (
        "urllib",
        "http.client",
        "requests",
        "socket",
        "ftplib",
        "smtplib",
        "telnetlib",
        "xmlrpc",
    )
    offenders: list[str] = []
    for path in iter_input_files(stage / PACKAGED_SRC):
        if path.suffix != ".py":
            continue
        for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            stripped = line.strip()
            if stripped.startswith("#"):
                continue
            for name in forbidden:
                if f"import {name}" in stripped:
                    offenders.append(f"{path.relative_to(stage)}:{lineno}:{name}")
    if offenders:
        raise BuildError("offline policy violated by imports: " + ", ".join(offenders))


# --------------------------------------------------------------------------- #
# Manifest / checksums
# --------------------------------------------------------------------------- #
def write_checksums(release_dir: Path) -> None:
    lines = [
        f"{sha256_file(path)}  {path.relative_to(release_dir).as_posix()}"
        for path in iter_input_files(release_dir)
        if path.name not in (CHECKSUMS_NAME, MANIFEST_SIDECAR)
    ]
    (release_dir / CHECKSUMS_NAME).write_text(
        "\n".join(sorted(lines)) + "\n", encoding="utf-8"
    )


def build_manifest(
    release_dir: Path,
    release_id: str,
    version: str,
    groups: dict[str, dict[str, Any]],
    test_summary: dict[str, Any],
    imported: dict[str, str],
    runtime: dict[str, Any],
) -> dict[str, Any]:
    artifacts = {
        path.relative_to(release_dir).as_posix(): sha256_file(path)
        for path in iter_input_files(release_dir)
        if path.name not in (MANIFEST_NAME, MANIFEST_SIDECAR, CHECKSUMS_NAME)
    }
    manifest = {
        "manifest_version": 1,
        "release_id": release_id,
        "project": "carbon-factor-ledger",
        "project_version": version,
        "built_offline": True,
        "determinism": {
            "python_isolated": True,
            "python_hash_seed": 0,
            "timezone": "UTC",
            "source_date_epoch": 0,
            "canonical_json": "sort_keys=true,separators=(',',':')",
        },
        "runtime": runtime,
        "hashes": {
            "source": groups["source"],
            "input": groups["input"],
            "rules": groups["rules"],
            "tests": groups["tests"],
        },
        "test_summary": {
            "tests_run": test_summary["tests_run"],
            "failures": test_summary["failures"],
            "errors": test_summary["errors"],
            "skipped": test_summary["skipped"],
            "successful": test_summary["successful"],
            "test_ids": test_summary["test_ids"],
        },
        "module_imports": imported,
        "artifacts": dict(sorted(artifacts.items())),
        "artifact_count": len(artifacts),
    }
    return manifest


def write_manifest(release_dir: Path, manifest: dict[str, Any]) -> None:
    serialized = json.dumps(manifest, sort_keys=True, ensure_ascii=False, indent=2) + "\n"
    (release_dir / MANIFEST_NAME).write_text(serialized, encoding="utf-8")
    digest = sha256_bytes(serialized.encode("utf-8"))
    (release_dir / MANIFEST_SIDECAR).write_text(f"{digest}  {MANIFEST_NAME}\n", encoding="utf-8")


# --------------------------------------------------------------------------- #
# Build
# --------------------------------------------------------------------------- #
def command_build(args: argparse.Namespace) -> int:
    version = read_project_version()

    # Reject caches in the source tree instead of silently packaging them.
    for tree in (SRC_DIR, FIXTURES_DIR, TESTS_DIR):
        ensure_clean_tree(tree)

    source_hashes = tree_hashes(SRC_DIR)
    fixture_hashes = tree_hashes(FIXTURES_DIR)
    test_hashes = tree_hashes(TESTS_DIR)
    source_digest = canonical_digest(
        {**source_hashes, **fixture_hashes, "pyproject.toml": sha256_file(PYPROJECT)}
    )
    release_id = f"{version}-{source_digest[:12]}"

    releases_root = Path(args.release_root).resolve()
    target = releases_root / release_id

    if target.exists():
        # Idempotent rebuild: never overwrite.  Verify the existing release and
        # report it as the current authoritative artifact.
        print(f"[build] release {release_id} already exists; verifying instead of overwriting")
        return command_verify_namespace(target, releases_root)

    work_root = releases_root / "_work"
    work_root = releases_root / "_work"
    with staging_area(work_root, release_id) as stage:
        print(f"[build] staging at {stage.path}")
        copy_tree(SRC_DIR, stage / PACKAGED_SRC)
        copy_tree(FIXTURES_DIR, stage / PACKAGED_FIXTURES)
        copy_tree(TESTS_DIR, stage / PACKAGED_TESTS)
        shutil.copy2(PYPROJECT, stage / "pyproject.toml")

        print("[build] stage 1/6: offline import policy + module import check")
        check_offline_sources(stage)
        imported = run_import_check(stage)

        if args.skip_tests:
            # Explicit escape hatch for failure-injection / rule-mutation
            # rebuilds only.  The manifest records that tests were skipped, so
            # such a release can never masquerade as a fully verified one.
            print("[build] stage 2/6: unit tests SKIPPED (--skip-tests, test-only)")
            test_summary = {
                "tests_run": 0,
                "failures": 0,
                "errors": 0,
                "skipped": -1,
                "successful": False,
                "test_ids": [],
                "note": "tests explicitly skipped via --skip-tests",
            }
            (stage / "test_output.log").write_text(
                "unit tests skipped: build invoked with --skip-tests\n", encoding="utf-8"
            )
        else:
            print("[build] stage 2/6: unit tests (isolated interpreter)")
            test_summary = run_unit_tests(stage)

        print("[build] stage 3/6: deterministic footprint CLI run")
        footprint = run_footprint(stage)
        footprint_bytes = footprint.read_bytes()

        print("[build] stage 4/6: repeat-run determinism check")
        rerun = stage / "footprint.rerun.json"
        code = cli_launcher(
            str(stage / PACKAGED_SRC),
            [
                str(stage / PACKAGED_FIXTURES / "input.json"),
                str(stage / PACKAGED_FIXTURES / "rules"),
                str(rerun),
            ],
        )
        second = isolated_python(code, cwd=stage, extra_env={"TZ": args.timezone})
        if second.returncode != 0:
            raise BuildError(f"determinism rerun failed: {second.stderr.strip()}")
        if rerun.read_bytes() != footprint_bytes:
            raise BuildError("footprint output is not byte-identical across repeated runs")
        rerun.unlink()

        print("[build] stage 5/6: manifest + checksum assembly")
        runtime = {
            "python_executable": sys.executable,
            "python_version": sys.version.split()[0],
            "python_implementation": sys.implementation.name,
            "platform": sys.platform,
            "byteorder": sys.byteorder,
            "float_info": {
                "max": sys.float_info.max,
                "epsilon": sys.float_info.epsilon,
                "radix": sys.float_info.radix,
            },
            "timezone_forced": "UTC",
        }
        groups = {
            "source": hash_group(stage, [p for p in (stage / PACKAGED_SRC).rglob("*") if p.is_file()]),
            "input": hash_group(
                stage,
                [stage / PACKAGED_FIXTURES / "input.json"]
                + sorted((stage / PACKAGED_FIXTURES / "negative").glob("*.json")),
            ),
            "rules": hash_group(
                stage, [p for p in (stage / PACKAGED_FIXTURES / "rules").rglob("*") if p.is_file()]
            ),
            "tests": hash_group(stage, [p for p in (stage / PACKAGED_TESTS).rglob("*") if p.is_file()]),
        }
        manifest = build_manifest(
            stage, release_id, version, groups, test_summary, imported, runtime
        )
        # Status marker must exist before checksums are computed so that it is
        # itself covered by SHA256SUMS and the manifest artifact list.
        (stage / STATUS_NAME).write_text(
            f"release_id={release_id}\n", encoding="utf-8"
        )
        write_manifest(stage, manifest)
        write_checksums(stage)

        print("[build] stage 6/6: independent verification of staged release")
        verify_release_tree(stage)

        # Re-verify the target slot after staging, then publish atomically.
        if target.exists():
            raise BuildError(
                f"release {release_id} appeared concurrently; refusing to overwrite {target}"
            )
        releases_root.mkdir(parents=True, exist_ok=True)
        # os.replace moves the whole verified tree out of the throw-away stage
        # in one atomic rename; nothing partial is ever visible at ``target``.
        os.replace(stage.path, target)
        stage.published = True

    _point_current(releases_root, target, release_id)
    removed = prune_caches(REPO_ROOT)
    if removed:
        print(f"[build] cleaned temporary cache paths: {', '.join(removed)}")
    print(f"[build] published {target}")
    print(f"[build] {CURRENT_LINK} -> {target.name}")
    return 0


def _point_current(releases_root: Path, target: Path, release_id: str) -> None:
    current = releases_root / CURRENT_LINK
    tmp_link = releases_root / f".{CURRENT_LINK}.{release_id}.tmp"
    if tmp_link.exists() or tmp_link.is_symlink():
        tmp_link.unlink()
    os.symlink(target.name, tmp_link)
    os.replace(tmp_link, current)


# --------------------------------------------------------------------------- #
# Verify
# --------------------------------------------------------------------------- #
def verify_release_tree(release_dir: Path) -> dict[str, Any]:
    release_dir = release_dir.resolve()
    if not release_dir.is_dir():
        raise BuildError(f"release directory not found: {release_dir}")
    status_file = release_dir / STATUS_NAME
    if not status_file.is_file():
        raise BuildError(f"release is missing {STATUS_NAME}; refusing to treat it as complete")

    manifest_path = release_dir / MANIFEST_NAME
    sidecar = release_dir / MANIFEST_SIDECAR
    if not manifest_path.is_file() or not sidecar.is_file():
        raise BuildError("release is missing manifest.json / manifest.sha256")

    expected_sidecar = sidecar.read_text(encoding="utf-8").split()[0]
    actual_manifest_hash = sha256_file(manifest_path)
    if expected_sidecar != actual_manifest_hash:
        raise BuildError(
            f"manifest hash mismatch: sidecar={expected_sidecar} actual={actual_manifest_hash}"
        )
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))

    # 1. Every checksum line must recompute correctly.
    checksum_lines = (release_dir / CHECKSUMS_NAME).read_text(encoding="utf-8").splitlines()
    checked: dict[str, str] = {}
    for line in checksum_lines:
        if not line.strip():
            continue
        digest, rel = line.split("  ", 1)
        path = release_dir / rel
        if not path.is_file():
            raise BuildError(f"checksummed artifact missing: {rel}")
        actual = sha256_file(path)
        if actual != digest:
            raise BuildError(f"artifact hash mismatch for {rel}: {digest} != {actual}")
        checked[rel] = digest

    # 2. No unlisted file may exist (metadata bookkeeping files are exempt).
    exempt = {CHECKSUMS_NAME, MANIFEST_SIDECAR}
    on_disk = {
        path.relative_to(release_dir).as_posix()
        for path in iter_input_files(release_dir)
        if path.name not in exempt
    }
    listed = set(checked) | {MANIFEST_NAME}
    unlisted = on_disk - listed
    if unlisted:
        raise BuildError(f"unlisted files present in release: {sorted(unlisted)}")

    # 3. Manifest artifact hashes must match.
    for rel, digest in manifest["artifacts"].items():
        actual = sha256_file(release_dir / rel)
        if actual != digest:
            raise BuildError(f"manifest artifact hash mismatch for {rel}")

    # 4. Re-run the packaged CLI from the packaged source and compare bytes.
    output = release_dir / "verify.footprint.json"
    code = cli_launcher(
        str(release_dir / PACKAGED_SRC),
        [
            str(release_dir / PACKAGED_FIXTURES / "input.json"),
            str(release_dir / PACKAGED_FIXTURES / "rules"),
            str(output),
        ],
    )
    proc = isolated_python(code, cwd=release_dir)
    if proc.returncode != 0:
        raise BuildError(f"verify CLI run failed: {proc.stderr.strip()}")
    packaged = (release_dir / FOOTPRINT_NAME).read_bytes()
    if output.read_bytes() != packaged:
        raise BuildError("verify CLI run does not reproduce the packaged footprint byte-for-byte")
    output.unlink()

    # 5. Re-run the packaged domain tests from the packaged source.
    skipped_marker = manifest["test_summary"].get("skipped") == -1
    if not skipped_marker:
        test_code = (
            "import sys, unittest; "
            f"sys.path.insert(0, {str(release_dir / PACKAGED_SRC)!r}); "
            f"suite = unittest.TestLoader().discover({str(release_dir / PACKAGED_TESTS)!r}, "
            f"pattern='test_core.py', top_level_dir={str(release_dir)!r}); "
            "result = unittest.TextTestRunner(verbosity=1).run(suite); "
            "raise SystemExit(0 if result.wasSuccessful() else 1)"
        )
        test_proc = isolated_python(test_code, cwd=release_dir)
        if test_proc.returncode != 0:
            raise BuildError("packaged test suite fails during verification")

    return manifest


def command_verify_namespace(release_dir: Path, releases_root: Path | None = None) -> int:
    manifest = verify_release_tree(release_dir)
    print(
        f"[verify] OK release={manifest['release_id']} "
        f"tests={manifest['test_summary']['tests_run']} "
        f"artifacts={manifest['artifact_count']}"
    )
    if manifest["test_summary"].get("skipped") == -1:
        print("[verify] WARNING: release was built with --skip-tests; not rollback-eligible")
    if releases_root is not None:
        current = releases_root / CURRENT_LINK
        if current.is_symlink() and current.resolve() != release_dir.resolve():
            print(f"[verify] note: {CURRENT_LINK} points at {current.resolve().name}")
    return 0


def command_verify(args: argparse.Namespace) -> int:
    release_dir = Path(args.release_dir)
    return command_verify_namespace(release_dir)


# --------------------------------------------------------------------------- #
# Rollback
# --------------------------------------------------------------------------- #
def command_rollback(args: argparse.Namespace) -> int:
    releases_root = Path(args.release_root).resolve()
    target_name = args.release_id
    target = releases_root / target_name
    if not target.is_dir():
        raise BuildError(f"rollback target does not exist: {target}")
    # Never point current at an unverified release.
    manifest = verify_release_tree(target)
    if manifest["test_summary"].get("skipped") == -1:
        raise BuildError(
            f"release {target_name} was built with --skip-tests and is not rollback-eligible"
        )
    _point_current(releases_root, target, target_name)
    print(f"[rollback] {CURRENT_LINK} -> {target_name}")
    return 0


# --------------------------------------------------------------------------- #
# Diff
# --------------------------------------------------------------------------- #
def command_diff(args: argparse.Namespace) -> int:
    left_dir, right_dir = Path(args.left), Path(args.right)
    # Both sides must be complete, verified-style release directories before
    # any comparison; a partial/forged directory is rejected up front.
    left_manifest = verify_release_tree(left_dir)
    right_manifest = verify_release_tree(right_dir)
    left = left_manifest
    right = right_manifest

    report: dict[str, Any] = {
        "left": {"release_id": left["release_id"], "path": str(left_dir)},
        "right": {"release_id": right["release_id"], "path": str(right_dir)},
        "changed_sections": [],
        "rule_binding_changed": False,
        "footprint_changed": False,
        "file_changes": {"added": [], "removed": [], "modified": [], "unchanged": []},
    }

    for section in ("runtime", "hashes", "test_summary", "module_imports"):
        if left.get(section) != right.get(section):
            report["changed_sections"].append(section)

    left_art, right_art = left["artifacts"], right["artifacts"]
    report["file_changes"]["added"] = sorted(set(right_art) - set(left_art))
    report["file_changes"]["removed"] = sorted(set(left_art) - set(right_art))
    modified = sorted(
        rel for rel in set(left_art) & set(right_art) if left_art[rel] != right_art[rel]
    )
    report["file_changes"]["modified"] = modified
    report["file_changes"]["unchanged"] = sorted(
        rel for rel in set(left_art) & set(right_art) if left_art[rel] == right_art[rel]
    )

    left_fp = json.loads((left_dir / FOOTPRINT_NAME).read_text(encoding="utf-8"))
    right_fp = json.loads((right_dir / FOOTPRINT_NAME).read_text(encoding="utf-8"))
    report["rule_binding_changed"] = (
        left_fp.get("rule_binding") != right_fp.get("rule_binding")
    )
    report["footprint_changed"] = left_fp != right_fp
    if report["footprint_changed"]:
        report["footprint_field_diff"] = _field_diff(left_fp, right_fp)

    print(json.dumps(report, sort_keys=True, ensure_ascii=False, indent=2))
    return 0 if not report["footprint_changed"] else 1


def _field_diff(left: dict[str, Any], right: dict[str, Any]) -> dict[str, Any]:
    keys = sorted(set(left) | set(right))
    return {
        key: {"left": left.get(key), "right": right.get(key)}
        for key in keys
        if left.get(key) != right.get(key)
    }


# --------------------------------------------------------------------------- #
# Entry point
# --------------------------------------------------------------------------- #
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    build = sub.add_parser("build", help="build a reproducible offline release")
    build.add_argument("--release-root", default=str(REPO_ROOT / "releases"))
    build.add_argument(
        "--timezone",
        default="America/Los_Angeles",
        help="timezone used for the determinism rerun (build itself always forces UTC)",
    )
    build.add_argument(
        "--skip-tests",
        action="store_true",
        help="test-only escape hatch for failure-injection rebuilds; recorded in "
        "the manifest and rejected as an unverified release by rollback/verify policy",
    )
    build.set_defaults(func=command_build)

    verify = sub.add_parser("verify", help="verify an existing release directory")
    verify.add_argument("release_dir")
    verify.set_defaults(func=command_verify)

    rollback = sub.add_parser("rollback", help="point 'current' at a prior verified release")
    rollback.add_argument("--release-root", default=str(REPO_ROOT / "releases"))
    rollback.add_argument("release_id")
    rollback.set_defaults(func=command_rollback)

    diff = sub.add_parser("diff", help="compare two release directories (exit 1 on footprint change)")
    diff.add_argument("left")
    diff.add_argument("right")
    diff.set_defaults(func=command_diff)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except BuildError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
