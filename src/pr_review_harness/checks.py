"""Run bounded checks on exported immutable snapshots."""

import os
import signal
import subprocess
import sys
import tempfile
from pathlib import Path, PurePosixPath

from pr_review_harness.models import CheckRun, Evidence
from pr_review_harness.snapshot import Snapshot

_UNITTEST_DRIVER = """
import importlib.util
import sys
import traceback
import unittest
sys.path.insert(0, sys.argv[2])
try:
    spec = importlib.util.spec_from_file_location("harness_check", sys.argv[1])
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
except Exception:
    traceback.print_exc()
    sys.exit(2)
result = unittest.TextTestRunner(verbosity=2).run(
    unittest.defaultTestLoader.loadTestsFromModule(module)
)
if not result.testsRun:
    sys.exit(3)
sys.exit(2 if result.errors else 1 if result.failures else 0)
"""


class CheckRunner:
    """Expose syntax checks and optional local unittest execution."""

    def __init__(self, snapshot: Snapshot, *, run_tests: bool = False, timeout: int = 20):
        self.snapshot = snapshot
        self.run_tests = run_tests
        self.timeout = timeout
        self.cache: dict[tuple[str, str], Evidence] = {}

    def run(self, kind: str, path: str) -> Evidence:
        if kind not in {"syntax", "unittest"}:
            raise ValueError("Only syntax and unittest checks are supported.")
        if kind == "unittest" and not self.run_tests:
            raise ValueError("unittest requires --run-tests: it executes repository code locally.")
        if path not in self.snapshot.head_files or not path.endswith(".py"):
            raise ValueError("Choose a tracked Python file from the head snapshot.")
        key = (kind, path)
        if key not in self.cache:
            self.cache[key] = Evidence(
                id=f"check-{len(self.cache) + 1:03d}",
                kind=kind,
                path=path,
                base=self._run_version(kind, path, "base"),
                head=self._run_version(kind, path, "head"),
                same_check=self._same_check(kind, path),
            )
        return self.cache[key]

    def _same_check(self, kind: str, path: str) -> bool:
        if kind == "syntax":
            return True
        try:
            return self.snapshot.read_file(path, "base") == self.snapshot.read_file(path, "head")
        except (ValueError, FileNotFoundError):
            return False

    def _run_version(self, kind: str, path: str, version: str) -> CheckRun:
        sha = self.snapshot.head_sha if version == "head" else self.snapshot.merge_base_sha
        try:
            source = self.snapshot.read_file(path, version)
        except (ValueError, FileNotFoundError) as exc:
            return CheckRun(version, sha, "unavailable", None, str(exc))
        if kind == "syntax":
            try:
                compile(source, path, "exec", dont_inherit=True)
            except (SyntaxError, ValueError) as exc:
                return CheckRun(version, sha, "failed", 1, str(exc))
            return CheckRun(version, sha, "passed", 0, "Python compilation succeeded.")
        with tempfile.TemporaryDirectory(prefix="pr-harness-check-") as tmp:
            root = Path(tmp)
            self.snapshot.export(version, root)
            return self._unittest(root, path, version, sha)

    def _unittest(self, root: Path, path: str, version: str, sha: str) -> CheckRun:
        test = PurePosixPath(path)
        # Load exactly the requested file; discovery could include changed same-name tests.
        command = [
            sys.executable,
            "-c",
            _UNITTEST_DRIVER,
            str(root / test),
            str(root),
        ]
        environment = {
            key: os.environ[key] for key in ("PATH", "LANG", "SYSTEMROOT") if key in os.environ
        }
        environment["PYTHONNOUSERSITE"] = "1"
        with tempfile.TemporaryFile() as log:
            process = subprocess.Popen(
                command,
                cwd=root,
                env=environment,
                stdout=log,
                stderr=log,
                start_new_session=True,
            )
            try:
                code = process.wait(timeout=self.timeout)
                status = {0: "passed", 1: "failed", 2: "error", 3: "unavailable"}.get(code, "error")
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, signal.SIGKILL)
                process.wait()
                code, status = None, "timeout"
            log.seek(0, os.SEEK_END)
            size = log.tell()
            log.seek(max(0, size - 8000))
            output = log.read().decode("utf-8", errors="replace")
        if "Ran 0 tests" in output:
            status = "unavailable"
        return CheckRun(version, sha, status, code, output)
