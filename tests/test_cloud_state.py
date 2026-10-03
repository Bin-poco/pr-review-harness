"""Trusted artifact recovery, real graph replay and committed SQLite WAL snapshots."""

import hashlib
import io
import json
import shutil
import sqlite3
import zipfile
from dataclasses import asdict, replace
from types import SimpleNamespace
from urllib.error import HTTPError

import pytest
from test_automation import EmptyReviewModel
from test_runtime import make_snapshot

from pr_review_harness.budget import BudgetPolicy
from pr_review_harness.checks import CheckRunner
from pr_review_harness.cli import _dispatch, parser
from pr_review_harness.cloud_state import (
    CloudContext,
    _download,
    download_checkpoint,
    export_checkpoint,
    restore_checkpoint,
    resume_selection,
    write_pointer,
)
from pr_review_harness.memory import MemoryStore
from pr_review_harness.persistence import RunStore, identity, runner_identity
from pr_review_harness.runtime import DemoChatModel, ReviewFailure, review
from pr_review_harness.verification import DemoVerifierModel, verify_report

PROFILE = {
    "verify": True,
    "verify_max_findings": 5,
    "no_memory": False,
    "github_memory": False,
    "github_memory_path": ".harness/feedback.json",
}
CONTEXT = CloudContext(
    "example/demo",
    42,
    101,
    1,
    "workflow_dispatch",
    ".github/workflows/pr-review.yml",
    "refs/heads/main",
    "a" * 40,
    "b" * 40,
)
ORIGIN = {"run_id": 101, "attempt": 1, "event": "workflow_dispatch", "head_sha": "a" * 40}


class SyntaxModel(DemoChatModel):
    fail: bool = False
    calls: int = 0

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        self.calls += 1
        if self.fail and self.calls == 3:
            raise RuntimeError("provider interrupted after the committed syntax check")
        result = super()._generate(messages, stop, run_manager, **kwargs)
        for call in result.generations[0].message.tool_calls:
            if call["name"] == "run_check":
                call["args"] = {"kind": "syntax", "path": "pricing.py"}
        return result


def source_for(snapshot):
    return {
        "kind": "github",
        "url": "https://github.com/example/demo/pull/12",
        "owner": "example",
        "repository": "demo",
        "number": 12,
        "repository_id": 42,
        "base_sha": snapshot.base_sha,
        "head_sha": snapshot.head_sha,
        "state": "open",
        "title": "Original title",
    }


def save(tmp_path, model, *, memory="", completed=True):
    snapshot = make_snapshot(tmp_path)
    source = source_for(snapshot)
    runs = tmp_path / "runs"
    options = {"runs_dir": runs, "run_id": "ci-101-1", "source": source}
    if completed:
        result = review(snapshot, model, memory=memory, **options)
    else:
        with pytest.raises(ReviewFailure) as caught:
            review(snapshot, model, memory=memory, **options)
        result = caught.value.partial
    pointer, archive = tmp_path / "pointer.json", tmp_path / "state/checkpoint.zip"
    write_pointer(pointer, CONTEXT, runs, options["run_id"], PROFILE)
    return snapshot, source, runs, options, result, pointer, archive


def recover(snapshot, source, runs, archive, model, **changes):
    return restore_checkpoint(
        changes.get("bundle", archive.read_bytes()),
        runs,
        changes.get("context", replace(CONTEXT, run_id=102)),
        ORIGIN,
        snapshot,
        changes.get(
            "expected",
            {
                **identity(snapshot, model, BudgetPolicy(), "ast", False, "live"),
                "incremental": {"enabled": False},
            },
        ),
        changes.get("profile", PROFILE),
        changes.get("current_source", source),
    )


@pytest.fixture
def archived(tmp_path):
    values = save(tmp_path, EmptyReviewModel())
    snapshot, source, runs, options, result, pointer, archive = values
    export_checkpoint(pointer, archive, CONTEXT)
    shutil.move(runs / options["run_id"], tmp_path / "old-run")
    return snapshot, source, runs, archive


def test_cloud_model_failure_preserves_check_memory_index_and_attempts(tmp_path, monkeypatch):
    # Repository identity stays local in this plumbing fixture; source carries the GitHub ID.
    snapshot = make_snapshot(tmp_path / "feedback")
    memory = MemoryStore(tmp_path / "memory.db")
    uid = memory.add(snapshot.repo_id, "Confirmed pricing rule", "maintainer")
    frozen = memory.freeze_snapshot(snapshot.repo_id, ["pricing.py"], 4000)
    source, runs = source_for(snapshot), tmp_path / "runs"
    options = {"runs_dir": runs, "run_id": "ci-101-1", "source": source}
    with pytest.raises(ReviewFailure):
        review(snapshot, SyntaxModel(fail=True), memory=frozen, **options)
    pointer, archive = tmp_path / "pointer.json", tmp_path / "state/checkpoint.zip"
    write_pointer(pointer, CONTEXT, runs, options["run_id"], PROFILE)
    saved = export_checkpoint(pointer, archive, CONTEXT)
    assert saved["budget_usage"]["model_attempts"] == 3
    shutil.move(runs / options["run_id"], tmp_path / "old-run")
    (tmp_path / "memory.db").unlink()
    recover(snapshot, source, runs, archive, SyntaxModel())

    def repeated(*a, **k):
        raise AssertionError("A committed check must replay without executing")

    monkeypatch.setattr(CheckRunner, "_run_version", repeated)
    resumed = review(snapshot, SyntaxModel(), memory="Changed feedback", resume=True, **options)
    assert resumed["resumed"] and len(resumed["trace"]) == 3
    assert resumed["context"]["memory_snapshot"]["records"][0]["id"] == uid
    assert resumed["evidence"][0]["id"] == "check-001"
    assert resumed["budget_usage"]["model_attempts"] == 4
    assert resumed["budget_usage"]["tool_attempts"] == 3
    assert len(resumed["context"]["assembly"]["requests"]) == 4
    assert any(
        origin["source"] == "read_code"
        for record in resumed["context"]["assembly"]["fragment_index"]["records"]
        for origin in record["origins"]
    )


def test_cloud_verifier_receipt_replay_and_completed_recovery(tmp_path, monkeypatch):
    snapshot, source, runs, options, first, pointer, archive = save(tmp_path, SyntaxModel())
    finish, interrupted = RunStore.finish, False

    def interrupt(self, key, payload):
        nonlocal interrupted
        finish(self, key, payload)
        if not interrupted and payload.get("stage") == "verification_session":
            interrupted = True
            raise OSError("verifier receipt committed before graph checkpoint")

    monkeypatch.setattr(RunStore, "finish", interrupt)
    failed = verify_report(snapshot, first, DemoVerifierModel())
    assert failed["verification"]["status"] == "failed"
    monkeypatch.setattr(RunStore, "finish", finish)
    export_checkpoint(pointer, archive, CONTEXT)
    shutil.move(runs / options["run_id"], tmp_path / "failed-run")
    recover(snapshot, source, runs, archive, SyntaxModel())
    primary = review(snapshot, SyntaxModel(), resume=True, **options)
    restored = verify_report(snapshot, primary, DemoVerifierModel())
    assert restored["verification"]["status"] == "completed"
    assert restored["budget_usage"]["tool_attempts"] == 6
    assert [
        e["arguments"]["version"]
        for e in restored["verification"]["trace"]
        if e["tool"] == "read_code"
    ] == ["head", "base"]
    export_checkpoint(pointer, archive, CONTEXT)
    shutil.move(runs / options["run_id"], tmp_path / "completed-run")
    recover(snapshot, source, runs, archive, SyntaxModel())
    again = verify_report(
        snapshot, review(snapshot, SyntaxModel(), resume=True, **options), DemoVerifierModel()
    )
    assert again["budget_usage"] == restored["budget_usage"]
    assert again["verification"]["context"] == restored["verification"]["context"]
    assert again["context"]["assembly"] == restored["context"]["assembly"]


def test_export_includes_committed_wal_and_refuses_active_run(tmp_path):
    _, _, runs, options, _, pointer, archive = save(tmp_path, EmptyReviewModel())
    store = RunStore(runs, options["run_id"])
    with store.locked(), pytest.raises(ValueError, match="already active"):
        export_checkpoint(pointer, archive, CONTEXT)
    with sqlite3.connect(store.path / "checkpoint.sqlite3") as writer:
        writer.execute("PRAGMA journal_mode=WAL")
        writer.execute("PRAGMA wal_autocheckpoint=0")
        writer.execute("CREATE TABLE wal_probe (value TEXT)")
        writer.execute("INSERT INTO wal_probe VALUES ('committed')")
        writer.commit()
        assert (store.path / "checkpoint.sqlite3-wal").exists()
        export_checkpoint(pointer, archive, CONTEXT)
        with zipfile.ZipFile(archive) as bundle:
            target = tmp_path / "backed-up.sqlite3"
            target.write_bytes(bundle.read("checkpoint.sqlite3"))
        with sqlite3.connect(target) as backup:
            assert backup.execute("SELECT value FROM wal_probe").fetchone() == ("committed",)


@pytest.mark.parametrize(
    "change",
    [
        "pr",
        "head",
        "model",
        "budget",
        "source",
        "runner",
        "profile",
        "harness",
        "workflow",
        "attempt",
    ],
)
def test_incompatible_cloud_state_refused_before_install(archived, change):
    snapshot, source, runs, archive = archived
    manifest = json.loads(zipfile.ZipFile(archive).read("manifest.json"))
    expected = {
        key: manifest[key]
        for key in identity(snapshot, EmptyReviewModel(), BudgetPolicy(), "ast", False, "live")
    }
    changes = {"expected": expected}
    if change in {"pr", "head"}:
        changes["current_source"] = {**source, "number" if change == "pr" else "head_sha": 13}
        if change == "head":
            snapshot = SimpleNamespace(**{**snapshot.__dict__, "head_sha": "c" * 40})
    elif change in {"model", "budget", "source", "runner"}:
        expected[{"source": "implementation_sha256"}.get(change, change)] = "changed"
    elif change == "profile":
        changes["profile"] = {**PROFILE, "no_memory": True}
    else:
        changes["context"] = replace(
            CONTEXT,
            **{
                {"harness": "harness_sha", "workflow": "workflow_path", "attempt": "attempt"}[
                    change
                ]: "c" * 40 if change == "harness" else "other.yml" if change == "workflow" else 2
            },
        )
        if change == "attempt":
            # A new destination attempt is fine; producer attempt must match the source.
            raw = rewrite_archive(
                archive, metadata_change={"producer": {**asdict(CONTEXT), "attempt": 2}}
            )
            changes["bundle"] = raw
    with pytest.raises(ValueError):
        recover(snapshot, source, runs, archive, EmptyReviewModel(), **changes)
    assert not (runs / "ci-101-1").exists()


def rewrite_archive(archive, *, extra=None, corrupt=None, metadata_change=None):
    output = io.BytesIO()
    with zipfile.ZipFile(archive) as old, zipfile.ZipFile(output, "w") as new:
        for name in old.namelist():
            raw = old.read(name)
            if name == corrupt:
                raw += b"tampered"
            if name == "cloud-state.json" and metadata_change:
                raw = json.dumps({**json.loads(raw), **metadata_change}).encode()
            new.writestr(name, raw)
        if extra:
            new.writestr(extra, b"unexpected")
    return output.getvalue()


@pytest.mark.parametrize("mutation", ["traversal", "hash", "duplicate", "frozen"])
def test_corrupt_bundle_refused_before_checkpoint_loading(archived, mutation):
    snapshot, source, runs, archive = archived
    bundle = rewrite_archive(
        archive,
        extra="../escape"
        if mutation == "traversal"
        else "checkpoint.sqlite3"
        if mutation == "duplicate"
        else None,
        corrupt="checkpoint.sqlite3"
        if mutation == "hash"
        else "artifacts.json"
        if mutation == "frozen"
        else None,
    )
    with pytest.raises(ValueError):
        recover(snapshot, source, runs, archive, EmptyReviewModel(), bundle=bundle)
    assert not (runs / "ci-101-1").exists()


def test_restore_keeps_original_title_and_refuses_existing_run(archived):
    snapshot, source, runs, archive = archived
    manifest, origin = recover(
        snapshot,
        source,
        runs,
        archive,
        EmptyReviewModel(),
        current_source={**source, "title": "New"},
    )
    assert manifest["source"]["title"] == "Original title"
    assert origin["harness_run_id"] == "ci-101-1"
    with pytest.raises(ValueError, match="overwrite"):
        recover(snapshot, source, runs, archive, EmptyReviewModel())


class ArtifactClient:
    def __init__(self):
        self.repository = {"id": 42, "default_branch": "main"}
        self.run = {
            "id": 101,
            "run_attempt": 1,
            "status": "completed",
            "repository": {"id": 42},
            "workflow_id": 7,
            "path": CONTEXT.workflow_path,
            "event": "workflow_dispatch",
            "head_branch": "main",
            "head_repository": {"id": 42},
            "head_sha": "a" * 40,
        }
        stream = io.BytesIO()
        with zipfile.ZipFile(stream, "w") as archive:
            archive.writestr("checkpoint.zip", b"inner")
        self.raw = stream.getvalue()
        self.artifact = {
            "id": 99,
            "name": "harness-state-101-1",
            "expired": False,
            "workflow_run": {"id": 101, "repository_id": 42},
            "size_in_bytes": len(self.raw),
            "digest": "sha256:" + hashlib.sha256(self.raw).hexdigest(),
        }

    def request(self, method, endpoint):
        if "/actions/workflows/" in endpoint:
            return {"id": 7}
        if "/attempts/" in endpoint:
            return self.run
        if "/actions/artifacts?" in endpoint:
            return {"total_count": 1, "artifacts": [self.artifact]}
        return self.repository


@pytest.mark.parametrize(
    "change",
    [
        None,
        "fork",
        "workflow",
        "event",
        "expired",
        "digest",
        "default_branch",
        "run",
        "attempt",
        "in_progress",
    ],
)
def test_artifact_api_provenance_and_digest_validation(monkeypatch, change):
    client = ArtifactClient()
    if change == "fork":
        client.run["head_repository"]["id"] = 666
    elif change in {"workflow", "event", "attempt", "in_progress"}:
        key, value = {
            "workflow": ("workflow_id", 9),
            "event": ("event", "pull_request"),
            "attempt": ("run_attempt", 2),
            "in_progress": ("status", "in_progress"),
        }[change]
        client.run[key] = value
    elif change == "expired":
        client.artifact["expired"] = True
    elif change == "digest":
        client.artifact["digest"] = "sha256:changed"
    elif change == "default_branch":
        client.repository["default_branch"] = "untrusted"
    elif change == "run":
        client.artifact["workflow_run"]["id"] = 999
    monkeypatch.setattr("pr_review_harness.cloud_state._download", lambda *a: client.raw)
    if change:
        with pytest.raises(ValueError):
            download_checkpoint(client, replace(CONTEXT, run_id=102), (101, 1))
    else:
        bundle, origin = download_checkpoint(client, replace(CONTEXT, run_id=102), (101, 1))
        assert bundle == b"inner" and origin["artifact_id"] == 99


def test_signed_storage_redirect_never_receives_github_token():
    requests = []

    def open_request(request, **kwargs):
        requests.append(request)
        if len(requests) == 1:
            raise HTTPError(
                request.full_url,
                302,
                "Found",
                {"Location": "https://results.blob.core.windows.net/checkpoint?signed=1"},
                None,
            )
        return io.BytesIO(b"archive")

    client = SimpleNamespace(token="local-test-token", opener=SimpleNamespace(open=open_request))
    assert _download(client, "/artifact/zip") == b"archive"
    assert requests[0].get_header("Authorization") == "Bearer local-test-token"
    assert requests[1].get_header("Authorization") is None


def test_redirect_to_untrusted_host_refused():
    def open_request(request, **kwargs):
        raise HTTPError(
            request.full_url,
            302,
            "Found",
            {"Location": "https://results.blob.core.windows.net.evil.test/checkpoint"},
            None,
        )

    with pytest.raises(ValueError, match="host"):
        _download(SimpleNamespace(token="test", opener=SimpleNamespace(open=open_request)), "/a")


def test_rerun_refused_and_runner_ignores_kernel(monkeypatch):
    assert resume_selection(CONTEXT) is None
    with pytest.raises(ValueError, match="new Run workflow"):
        resume_selection(replace(CONTEXT, attempt=3), "999", "1")
    assert resume_selection(CONTEXT, "100", "2") == (100, 2)
    with pytest.raises(ValueError):
        resume_selection(CONTEXT, "101")
    before = runner_identity()
    monkeypatch.setattr("platform.platform", lambda: "another hosted kernel build")
    assert runner_identity() == before
    monkeypatch.setattr("pr_review_harness.persistence.version", lambda name: "changed")
    assert runner_identity() != before


def test_ci_pause_and_new_job_resume_use_frozen_run_without_memory_reload(tmp_path, monkeypatch):
    snapshot = make_snapshot(tmp_path)
    source = source_for(snapshot)
    event = tmp_path / "event.json"
    event.write_text(
        json.dumps(
            {"repository": {"id": 42, "full_name": "example/demo"}, "inputs": {"pr_number": "12"}}
        )
    )
    context = [CONTEXT]
    monkeypatch.setattr(CloudContext, "from_environment", lambda *a: context[0])
    monkeypatch.setattr("pr_review_harness.cli.configured_client", lambda: object())
    monkeypatch.setattr("pr_review_harness.cli.fetch_snapshot", lambda *a: (snapshot, source))
    monkeypatch.setattr("pr_review_harness.cli._live_model", lambda *a: EmptyReviewModel())
    published = []

    def preview(report, client, root, *, send):
        assert not send and report["resumed"]
        published.append(report["run_id"])
        return {"status": "preview"}

    monkeypatch.setattr("pr_review_harness.cli.publish_report", preview)
    common = [
        "github-ci",
        "--event-file",
        str(event),
        "--repository",
        "example/demo",
        "--event-name",
        "workflow_dispatch",
        "--cloud-checkpoint",
        "--verify",
        "--out",
        str(tmp_path / "out"),
        "--runs-dir",
        str(tmp_path / "runs"),
        "--memory-db",
        str(tmp_path / "memory.db"),
    ]
    assert _dispatch(parser().parse_args([*common, "--pause-after-review"])) == 0
    assert not published
    assert json.loads((tmp_path / "out/automation.json").read_text())["status"] == "paused"
    pointer, archive = tmp_path / "out/cloud-run.json", tmp_path / "state/checkpoint.zip"
    export_checkpoint(pointer, archive, CONTEXT)
    bundle = archive.read_bytes()
    shutil.move(tmp_path / "runs/ci-101-1", tmp_path / "previous-run")
    context[0] = replace(CONTEXT, run_id=102)
    monkeypatch.setattr("pr_review_harness.cli.download_checkpoint", lambda *a: (bundle, ORIGIN))

    def unexpected(*a, **k):
        raise AssertionError("Recovery must not replace frozen feedback from a new store")

    monkeypatch.setattr(MemoryStore, "freeze_snapshot", unexpected)
    assert _dispatch(parser().parse_args([*common, "--resume-run-id", "101"])) == 0
    result = json.loads((tmp_path / "out/automation.json").read_text())
    assert result["status"] == "completed"
    assert result["restored_from"]["run_id"] == 101
    assert published == ["ci-101-1"]
    assert (
        json.loads((tmp_path / "out/review.json").read_text())["budget_usage"]["model_attempts"]
        == 1
    )


@pytest.mark.parametrize("event", ["pull_request", "push", "workflow_run"])
def test_environment_rejects_untrusted_event(monkeypatch, event):
    for key, value in {
        "GITHUB_REPOSITORY": "example/demo",
        "GITHUB_REPOSITORY_ID": "42",
        "GITHUB_WORKFLOW_REF": "example/demo/.github/workflows/pr-review.yml@refs/heads/main",
        "GITHUB_EVENT_NAME": event,
    }.items():
        monkeypatch.setenv(key, value)
    with pytest.raises(ValueError, match="trusted dispatch"):
        CloudContext.from_environment("example/demo", 42)
