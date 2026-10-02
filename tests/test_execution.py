"""Execution identity, deadline behavior and opt-in real Docker boundary checks."""

import json
import os
import subprocess
import sys
import time
from dataclasses import replace

import pytest

from pr_review_harness.checks import CheckRunner
from pr_review_harness.cli import _dispatch, parser
from pr_review_harness.demo import create_demo
from pr_review_harness.execution import ExecutionPolicy, bounded_process, docker_environment
from pr_review_harness.runtime import DemoChatModel, review
from pr_review_harness.snapshot import Snapshot


def test_remote_cli_rejects_host_tests_before_network(tmp_path, monkeypatch):
    def unexpected(*args, **kwargs):
        raise AssertionError("Remote access should not occur")

    monkeypatch.setattr("pr_review_harness.cli.configured_client", unexpected)
    args = parser().parse_args(
        [
            "github-review",
            "--pr",
            "https://github.com/example/demo/pull/1",
            "--run-tests",
            "--test-backend",
            "local",
        ]
    )
    with pytest.raises(ValueError, match="require.*docker"):
        _dispatch(args)
    assert (
        parser()
        .parse_args(
            [
                "review",
                "--repo",
                str(tmp_path),
                "--base",
                "main",
                "--head",
                "HEAD",
            ]
        )
        .test_backend
        == "docker"
    )


def test_docker_missing_image_fails_before_model_and_has_no_host_fallback(tmp_path, monkeypatch):
    base, head = create_demo(tmp_path / "repo")
    snapshot = Snapshot.load(tmp_path / "repo", base, head)
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: subprocess.CompletedProcess(a, 1))
    with pytest.raises(ValueError, match="preload"):
        review(
            snapshot, DemoChatModel(), run_tests=True, execution=ExecutionPolicy(backend="docker")
        )


def test_execution_policy_pins_image_and_platform(monkeypatch):
    image_id = "sha256:" + "a" * 64
    calls = []

    def inspect(command, **kwargs):
        calls.append(command)
        return subprocess.CompletedProcess(
            command,
            0,
            json.dumps(
                [
                    {"Id": image_id, "Os": "linux", "Architecture": "amd64"},
                ]
            ).encode(),
        )

    monkeypatch.setattr(subprocess, "run", inspect)
    policy = ExecutionPolicy(backend="docker").prepare()
    assert (policy.image_id, policy.platform) == (image_id, "linux/amd64")
    assert ExecutionPolicy.from_manifest(policy.manifest()).prepare() == policy
    assert calls[-1][-1] == image_id
    with pytest.raises(ValueError, match="platform changed"):
        replace(policy, platform="linux/arm64").prepare()


def test_execution_limits_are_in_resume_identity(tmp_path):
    base, head = create_demo(tmp_path / "repo")
    snapshot = Snapshot.load(tmp_path / "repo", base, head)
    options = {"run_tests": True, "runs_dir": tmp_path / "runs", "run_id": "execution"}
    report = review(snapshot, DemoChatModel(), **options)
    assert report["run_manifest"]["execution"]["timeout"] == 20
    with pytest.raises(ValueError, match="execution"):
        review(
            snapshot, DemoChatModel(), resume=True, execution=ExecutionPolicy(timeout=10), **options
        )


def test_log_tail_and_deadline_are_bounded(tmp_path):
    code, output = bounded_process(
        [sys.executable, "-c", "print('x'*5000000); print('TAIL')"],
        cwd=tmp_path,
        environment={},
        timeout=5,
        output_bytes=2000,
    )
    assert code == 0 and output.endswith("TAIL\n") and len(output.encode()) == 2000
    code, _ = bounded_process(
        [sys.executable, "-c", "while True: print('x'*1000, flush=True)"],
        cwd=tmp_path,
        environment={},
        timeout=1,
        output_bytes=2000,
    )
    assert code is None


def test_docker_cli_environment_does_not_forward_api_keys(monkeypatch):
    for key in ("HARNESS_API_KEY", "GH_TOKEN", "GITHUB_TOKEN", "HARNESS_GITHUB_TOKEN"):
        monkeypatch.setenv(key, "test-secret")
        assert key not in docker_environment()


docker_required = pytest.mark.skipif(
    os.getenv("HARNESS_TEST_DOCKER") != "1",
    reason="Opt in with HARNESS_TEST_DOCKER=1 and preload image",
)


@docker_required
def test_real_docker_regression_and_completed_resume(tmp_path):
    base, head = create_demo(tmp_path / "repo")
    snapshot = Snapshot.load(tmp_path / "repo", base, head)
    options = {
        "run_tests": True,
        "execution": ExecutionPolicy(backend="docker"),
        "runs_dir": tmp_path / "runs",
        "run_id": "docker",
    }
    report = review(snapshot, DemoChatModel(), **options)
    check = next(item for item in report["evidence"] if item["kind"] == "unittest")
    assert (check["base"]["status"], check["head"]["status"]) == ("passed", "failed")
    assert report["run_manifest"]["execution"]["image_id"].startswith("sha256:")
    resumed = review(snapshot, DemoChatModel(), resume=True, **options)
    assert resumed["evidence"] == report["evidence"]
    assert resumed["budget_usage"] == report["budget_usage"]


def _docker_fixture(tmp_path, source):
    repo = tmp_path / "repo"
    create_demo(repo)
    (repo / "boundary_test.py").write_text(source)
    subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
    subprocess.run(
        [
            "git",
            "-C",
            str(repo),
            "-c",
            "core.hooksPath=/dev/null",
            "commit",
            "-qm",
            "boundary check",
        ],
        check=True,
    )
    return Snapshot.load(repo, "HEAD", "HEAD")


@docker_required
def test_real_docker_no_network_secret_or_host_writes(tmp_path, monkeypatch):
    monkeypatch.setenv("HARNESS_API_KEY", "host-only-sentinel")
    monkeypatch.setenv("GH_TOKEN", "host-only-sentinel")
    snapshot = _docker_fixture(
        tmp_path,
        """import os
import socket
import unittest
from pathlib import Path

class BoundaryTests(unittest.TestCase):
    def test_boundary(self):
        self.assertNotEqual(os.getuid(), 0)
        self.assertNotIn('HARNESS_API_KEY', os.environ)
        self.assertNotIn('GH_TOKEN', os.environ)
        self.assertFalse(Path('/var/run/docker.sock').exists())
        status = Path('/proc/self/status').read_text()
        self.assertIn('NoNewPrivs:\t1', status)
        self.assertIn('CapEff:\t0000000000000000', status)
        cgroup = Path('/sys/fs/cgroup')
        self.assertEqual((cgroup / 'memory.max').read_text().strip(), '268435456')
        self.assertEqual((cgroup / 'pids.max').read_text().strip(), '64')
        for path in ('/workspace/host-write', '/root-write'):
            with self.assertRaises(OSError):
                Path(path).write_text('must fail')
        Path('/tmp/allowed').write_text('ok')
        with self.assertRaises(OSError):
            socket.create_connection(('1.1.1.1', 443), timeout=1)
""",
    )
    prior_umask = os.umask(0o077)
    try:
        result = CheckRunner(
            snapshot, run_tests=True, execution=ExecutionPolicy(backend="docker")
        ).run("unittest", "boundary_test.py")
    finally:
        os.umask(prior_umask)
    assert result.head.status == "passed", result.head.output
    assert not (snapshot.repo / "host-write").exists()


@docker_required
def test_real_docker_timeout_cleans_child_and_container(tmp_path):
    snapshot = _docker_fixture(
        tmp_path,
        """import subprocess
import sys
import unittest

class TimeoutTests(unittest.TestCase):
    def test_forever(self):
        subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(600)'])
        while True:
            print('x' * 10000, flush=True)
""",
    )
    policy = ExecutionPolicy(backend="docker", timeout=1, output_bytes=2000)
    result = CheckRunner(snapshot, run_tests=True, execution=policy).run(
        "unittest", "boundary_test.py"
    )
    assert (result.base.status, result.head.status) == ("timeout", "timeout")
    assert result.head.exit_code is None and len(result.head.output.encode()) <= 2000
    live = subprocess.check_output(
        [
            "docker",
            "ps",
            "-aq",
            "--filter",
            "name=pr-harness-",
        ],
        text=True,
    )
    assert not live.strip()


@docker_required
def test_container_deadline_survives_killed_host_runner(tmp_path):
    snapshot = _docker_fixture(
        tmp_path,
        """import time
import unittest
class HangingTests(unittest.TestCase):
    def test_hang(self):
        time.sleep(600)
""",
    )
    driver = """from pathlib import Path
from pr_review_harness.checks import CheckRunner
from pr_review_harness.execution import ExecutionPolicy
from pr_review_harness.snapshot import Snapshot
import sys
s = Snapshot.load(Path(sys.argv[1]), 'HEAD', 'HEAD')
CheckRunner(s, run_tests=True, execution=ExecutionPolicy(backend='docker', timeout=3)).run(
    'unittest', 'boundary_test.py')
"""

    def containers():
        return set(
            subprocess.check_output(
                [
                    "docker",
                    "ps",
                    "-aq",
                    "--filter",
                    "name=pr-harness-",
                ],
                text=True,
            ).split()
        )

    prior = containers()
    worker = subprocess.Popen([sys.executable, "-c", driver, str(snapshot.repo)])
    deadline = time.monotonic() + 10
    created = set()
    try:
        while time.monotonic() < deadline:
            created = containers() - prior
            if created:
                break
            time.sleep(0.1)
        assert created, "Container did not start"
        worker.kill()
        worker.wait(timeout=5)
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline and created & containers():
            time.sleep(0.2)
        assert not created & containers(), "Orphan container exceeded its own deadline"
    finally:
        if worker.poll() is None:
            worker.kill()
            worker.wait(timeout=5)
        for container in created & containers():
            subprocess.run(["docker", "rm", "-f", container], capture_output=True, timeout=10)
