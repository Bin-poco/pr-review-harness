"""Run bounded checks on exported immutable snapshots."""

import hashlib
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from uuid import uuid4

from pr_review_harness.execution import ExecutionPolicy, bounded_process, docker_environment
from pr_review_harness.incremental import IncrementalStore
from pr_review_harness.models import CheckRun, Evidence
from pr_review_harness.persistence import digest
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

# A trusted PID 1 owns the deadline even if the host runner is killed. The test
# child cannot turn off this timer by changing Python state in its own process.
_CONTAINER_SUPERVISOR = """
import subprocess
import sys
p = subprocess.Popen([sys.executable, '-I', '-B', '-c', sys.argv[1],
                      sys.argv[2], '/workspace'])
try:
    sys.exit(p.wait(timeout=int(sys.argv[3])))
except subprocess.TimeoutExpired:
    print('Harness container test deadline exceeded.', flush=True)
    sys.exit(4)
"""


class CheckRunner:
    """Expose syntax checks and explicitly configured unittest execution."""

    def __init__(
        self,
        snapshot: Snapshot,
        *,
        run_tests: bool = False,
        timeout: int = 20,
        execution: ExecutionPolicy | None = None,
        reuse: IncrementalStore | None = None,
        run_id: str | None = None,
    ):
        self.snapshot = snapshot
        self.run_tests = run_tests
        self.execution = execution or ExecutionPolicy(timeout=timeout)
        if run_tests:
            self.execution = self.execution.prepare()
        self.timeout = self.execution.timeout
        self.reuse = reuse
        self.run_id = run_id
        self.syntax_identity = {
            "python": sys.version,
            "optimization": sys.flags.optimize,
            "implementation": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        }
        self.cache: dict[tuple[str, str], Evidence] = {}

    def run(self, kind: str, path: str) -> Evidence:
        if kind not in {"syntax", "unittest"}:
            raise ValueError("Only syntax and unittest checks are supported.")
        if kind == "unittest" and not self.run_tests:
            raise ValueError("unittest requires --run-tests: it executes repository code.")
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
            key = digest(
                {
                    "kind": kind,
                    "repo_id": self.snapshot.repo_id,
                    "path": path,
                    "source_sha256": hashlib.sha256(source.encode()).hexdigest(),
                    "compiler": self.syntax_identity,
                }
            )
            saved = self.reuse.get_check(key) if self.reuse else None
            if saved:
                return CheckRun(
                    version,
                    sha,
                    saved["status"],
                    saved["exit_code"],
                    saved["output"],
                    {
                        "status": "hit",
                        "key": key,
                        "origin_run_id": saved["run_id"],
                        "origin_sha": saved["sha"],
                    },
                )
            try:
                compile(source, path, "exec", dont_inherit=True)
                status, code, output = "passed", 0, "Python compilation succeeded."
            except (SyntaxError, ValueError) as exc:
                status, code, output = "failed", 1, str(exc)
            if self.reuse:
                self.reuse.put_check(
                    key,
                    {
                        "status": status,
                        "exit_code": code,
                        "output": output,
                        "run_id": self.run_id,
                        "sha": sha,
                    },
                )
            return CheckRun(
                version,
                sha,
                status,
                code,
                output,
                {"status": "miss", "key": key} if self.reuse else None,
            )
        with tempfile.TemporaryDirectory(prefix="pr-harness-check-") as tmp:
            root = Path(tmp)
            self.snapshot.export(version, root)
            return self._unittest(root, path, version, sha)

    def _unittest(self, root: Path, path: str, version: str, sha: str) -> CheckRun:
        if self.execution.backend == "docker":
            return self._docker_unittest(root, path, version, sha)
        command = [
            sys.executable,
            "-I",
            "-B",
            "-c",
            _UNITTEST_DRIVER,
            str(root / path),
            str(root),
        ]
        environment = {
            key: os.environ[key] for key in ("PATH", "LANG", "SYSTEMROOT") if key in os.environ
        }
        environment["PYTHONNOUSERSITE"] = "1"
        code, output = bounded_process(
            command,
            cwd=root,
            environment=environment,
            timeout=self.timeout,
            output_bytes=self.execution.output_bytes,
        )
        return self._result(version, sha, code, output)

    def _docker_unittest(self, root, path, version, sha):
        name = "pr-harness-" + uuid4().hex
        policy = self.execution
        # Keep the export private at mode 0700, including restrictive umasks.
        # Match the non-root host UID; root callers transfer only this export
        # to the unprivileged container UID instead of making it world-readable.
        if not os.getuid():
            for directory, _, files in os.walk(root):
                os.chown(directory, policy.uid, policy.gid)
                for filename in files:
                    os.chown(Path(directory) / filename, policy.uid, policy.gid)
        command = [
            "docker",
            "run",
            "--name",
            name,
            "--rm",
            "--pull=never",
            "--log-driver",
            "none",
            "--network",
            "none",
            "--read-only",
            "--cap-drop",
            "ALL",
            "--security-opt",
            "no-new-privileges",
            "--user",
            f"{policy.uid}:{policy.gid}",
            "--pids-limit",
            str(policy.pids),
            "--memory",
            f"{policy.memory_mb}m",
            "--memory-swap",
            f"{policy.memory_mb}m",
            "--cpus",
            str(policy.cpus),
            "--ulimit",
            "nofile=256:256",
            "--tmpfs",
            "/tmp:rw,noexec,nosuid,nodev,size=32m,mode=1777",
            "--mount",
            f"type=bind,source={root},target=/workspace,readonly",
            "--workdir",
            "/workspace",
            "--env",
            "HOME=/tmp",
            "--env",
            "TMPDIR=/tmp",
            "--entrypoint",
            "python",
            policy.image_id,
            "-I",
            "-B",
            "-c",
            _CONTAINER_SUPERVISOR,
            _UNITTEST_DRIVER,
            f"/workspace/{path}",
            str(policy.timeout),
        ]
        try:
            code, output = bounded_process(
                command,
                cwd=root,
                environment=docker_environment(),
                timeout=policy.timeout + 10,
                output_bytes=policy.output_bytes,
            )
        finally:
            # Also handles cancellation/failed starts. A killed host cannot enter
            # finally; the in-container deadline and --rm handle that case.
            cleanup = subprocess.run(
                ["docker", "rm", "-f", name],
                env=docker_environment(),
                capture_output=True,
                timeout=10,
            )
            if cleanup.returncode and b"No such container" not in cleanup.stderr:
                raise RuntimeError("Container cleanup failed; inspect Docker before retrying")
        return self._result(version, sha, code, output)

    @staticmethod
    def _result(version, sha, code, output):
        status = {
            0: "passed",
            1: "failed",
            2: "error",
            3: "unavailable",
            4: "timeout",
            None: "timeout",
        }.get(code, "error")
        if status == "timeout":
            code = None
        return CheckRun(version, sha, status, code, output)
