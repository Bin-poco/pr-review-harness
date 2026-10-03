"""Cross-job cache evidence, atomic validation, fallback and checkpoint interaction."""

import hashlib
import io
import json
import shutil
import zipfile
from dataclasses import replace

import pytest
from test_cloud_state import CONTEXT, ORIGIN, PROFILE, ArtifactClient, source_for
from test_incremental import SyntaxReviewModel, update
from test_incremental import repository as repository

from pr_review_harness.checks import CheckRunner
from pr_review_harness.cli import _dispatch, parser
from pr_review_harness.cloud_cache import (
    MAX_BYTES,
    MAX_ENTRIES,
    cache_identity,
    export_syntax_cache,
    import_syntax_cache,
    restore_latest_syntax_cache,
)
from pr_review_harness.cloud_state import (
    download_trusted_artifact,
    export_checkpoint,
    restore_checkpoint,
    write_pointer,
)
from pr_review_harness.incremental import IncrementalStore
from pr_review_harness.persistence import RunStore
from pr_review_harness.report import render_markdown
from pr_review_harness.runtime import review


@pytest.fixture
def bundle(repository, tmp_path):
    store = IncrementalStore(tmp_path / "producer")
    runner = CheckRunner(repository, reuse=store, run_id="ci-101-1")
    runner.run("syntax", "pricing.py")
    runner.run("syntax", "z_module.py")
    output = tmp_path / "syntax-cache.json"
    assert export_syntax_cache(store, CONTEXT, repository.repo_id, output)["entries"] == 3
    return output.read_bytes()


def test_new_jobs_reuse_compilation_but_run_new_models_and_rebind_versions(
    repository, tmp_path, monkeypatch
):
    first_store = IncrementalStore(tmp_path / "job-one")
    first_model = SyntaxReviewModel()
    first = review(
        repository,
        first_model,
        run_id="ci-101-1",
        syntax_cache=first_store,
        syntax_cache_source={"status": "cold"},
    )
    output = tmp_path / "syntax-cache.json"
    export_syntax_cache(first_store, CONTEXT, repository.repo_id, output)
    second_store = IncrementalStore(tmp_path / "job-two")
    origin = {**ORIGIN, "artifact_id": 99, "artifact_digest": "sha256:" + "a" * 64}
    assert (
        import_syntax_cache(
            output.read_bytes(),
            second_store,
            replace(CONTEXT, run_id=102),
            origin,
            repository.repo_id,
        )
        == 3
    )

    def repeated(*a, **k):
        raise AssertionError("Imported compilation must not run again")

    monkeypatch.setattr("pr_review_harness.checks.compile", repeated, raising=False)
    second_model = SyntaxReviewModel()
    second = review(
        repository,
        second_model,
        run_id="ci-102-1",
        syntax_cache=second_store,
        syntax_cache_source={"status": "imported", "origin": origin},
    )
    assert first_model.calls == second_model.calls == 2
    assert second["run_id"] != first["run_id"] and not second["resumed"]
    assert second["findings"][0]["id"] != first["findings"][0]["id"]
    assert second["syntax_cache"]["hits"] == 4 and second["syntax_cache"]["misses"] == 0
    assert second["incremental"]["enabled"] is False
    assert "云端语法检查缓存：命中 4 次" in render_markdown(second)
    for evidence in second["evidence"]:
        for version, sha in (("base", repository.merge_base_sha), ("head", repository.head_sha)):
            result = evidence[version]
            assert result["sha"] == sha
            assert result["cache"]["origin_run_id"] == "ci-101-1"
            assert result["cache"]["origin_artifact"] == origin
    with second_store.connect() as conn:
        assert conn.execute("SELECT count(*) FROM baselines").fetchone()[0] == 0


def test_changed_source_compiles_and_new_sha_rebinds_unchanged_file(
    bundle, repository, tmp_path, monkeypatch
):
    store = IncrementalStore(tmp_path / "consumer")
    import_syntax_cache(bundle, store, replace(CONTEXT, run_id=102), ORIGIN, repository.repo_id)
    changed = update(repository, "pricing.py", "def value():\n    return 2\n")
    calls = []

    def counted(source, path, *a, **k):
        calls.append(path)
        return compile(source, path, *a, **k)

    monkeypatch.setattr("pr_review_harness.checks.compile", counted, raising=False)
    runner = CheckRunner(changed, reuse=store, run_id="ci-102-1")
    pricing = runner.run("syntax", "pricing.py")
    unchanged = runner.run("syntax", "z_module.py")
    assert calls == ["pricing.py"]
    assert pricing.head.status == "passed" and pricing.head.cache["status"] == "miss"
    assert unchanged.head.sha == changed.head_sha
    assert unchanged.head.cache["origin_sha"] == repository.merge_base_sha
    assert unchanged.head.cache["status"] == "hit"


@pytest.mark.parametrize(
    "change",
    [
        "repo",
        "harness",
        "workflow",
        "attempt",
        "compiler",
        "key",
        "input_repo",
        "path",
        "source",
        "status",
        "code",
        "output",
        "run_id",
        "sha",
        "duplicate",
        "too_many",
        "extra",
        "missing",
        "malformed",
        "oversized",
        "duplicate_json_key",
    ],
)
def test_bad_cache_never_partially_installs(bundle, repository, tmp_path, change):
    data = json.loads(bundle)
    entry = data["entries"][-1]  # All earlier entries are valid, so validate-before-write matters.
    if change == "repo":
        data["repo_id"] = "f" * 64
    elif change in {"harness", "workflow", "attempt"}:
        key = {"harness": "harness_sha", "workflow": "workflow_path", "attempt": "attempt"}[change]
        data["producer"][key] = 2 if change == "attempt" else "changed"
    elif change == "compiler":
        data["compiler"]["python"] = "changed"
    elif change == "key":
        entry["key"] = "f" * 64
    elif change in {"input_repo", "path", "source"}:
        key = {"input_repo": "repo_id", "path": "path", "source": "source_sha256"}[change]
        entry["value"]["inputs"][key] = "../other.py" if change == "path" else "changed"
    elif change in {"status", "code", "output", "run_id", "sha"}:
        key = "exit_code" if change == "code" else change
        entry["value"][key] = {
            "status": {},
            "code": True,
            "output": "x" * 8001,
            "run_id": "../escape",
            "sha": "changed",
        }[change]
    elif change == "duplicate":
        data["entries"].append(data["entries"][0])
    elif change == "too_many":
        data["entries"] *= MAX_ENTRIES
    elif change == "extra":
        entry["value"]["baseline"] = {"old_findings": []}
    elif change == "missing":
        del data["producer"]
    raw = json.dumps(data).encode()
    if change == "malformed":
        raw = b"not json"
    elif change == "oversized":
        raw = b" " * (MAX_BYTES + 1)
    elif change == "duplicate_json_key":
        raw = b'{"schema_version":1,"schema_version":1}'
    store = IncrementalStore(tmp_path / "consumer")
    with pytest.raises(ValueError):
        import_syntax_cache(raw, store, replace(CONTEXT, run_id=102), ORIGIN, repository.repo_id)
    assert store.recent_checks(MAX_ENTRIES) == []


@pytest.mark.parametrize("bad", ["expired", "digest", "event", "workflow", "member", "size"])
def test_cache_artifact_reuses_provenance_gate(monkeypatch, bad):
    client = ArtifactClient()
    client.artifact["name"] = "harness-syntax-101-1"
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w") as archive:
        archive.writestr("other.json" if bad == "member" else "syntax-cache.json", b"{}")
    client.raw = stream.getvalue()
    client.artifact["digest"] = "sha256:" + hashlib.sha256(client.raw).hexdigest()
    if bad == "expired":
        client.artifact["expired"] = True
    elif bad == "digest":
        client.artifact["digest"] = "sha256:wrong"
    elif bad == "event":
        client.run["event"] = "pull_request"
    elif bad == "workflow":
        client.run["workflow_id"] = 999
    elif bad == "size":
        client.artifact["size_in_bytes"] = MAX_BYTES + 1
    monkeypatch.setattr("pr_review_harness.cloud_state._download", lambda *a: client.raw)
    with pytest.raises(ValueError):
        download_trusted_artifact(
            client,
            replace(CONTEXT, run_id=102),
            (101, 1),
            prefix="harness-syntax",
            member_name="syntax-cache.json",
            max_bytes=MAX_BYTES,
        )


def test_discovery_rejects_bad_recent_source_then_uses_valid_older_source(
    bundle, repository, tmp_path, monkeypatch
):
    client = ArtifactClient()
    client.request = lambda *a: {
        "workflow_runs": [
            {"id": 102, "run_attempt": 1, "conclusion": "success"},
            {"id": 101, "run_attempt": 1, "conclusion": "success"},
        ]
    }
    selections = []

    def download(client, context, selection, **kwargs):
        selections.append(selection)
        return (b"bad" if selection[0] == 102 else bundle), ORIGIN

    monkeypatch.setattr("pr_review_harness.cloud_cache.download_trusted_artifact", download)
    store = IncrementalStore(tmp_path / "consumer")
    result = restore_latest_syntax_cache(
        client, replace(CONTEXT, run_id=103), repository.repo_id, store
    )
    assert selections == [(102, 1), (101, 1)]
    assert result["status"] == "imported" and result["entries"] == 3
    assert result["rejected_sources"] == 1
    client.request = lambda *a: (_ for _ in ()).throw(RuntimeError("network unavailable"))
    assert (
        restore_latest_syntax_cache(client, CONTEXT, repository.repo_id, store)["status"]
        == "unavailable"
    )


def test_checkpoint_resume_needs_no_external_cache_and_retains_frozen_provenance(
    bundle, repository, tmp_path, monkeypatch
):
    store = IncrementalStore(tmp_path / "consumer")
    import_syntax_cache(bundle, store, replace(CONTEXT, run_id=102), ORIGIN, repository.repo_id)
    options = dict(runs_dir=tmp_path / "runs", run_id="ci-102-1", source=source_for(repository))
    receipt = {"status": "imported", "entries": 3, "origin": ORIGIN}
    first = review(
        repository, SyntaxReviewModel(), syntax_cache=store, syntax_cache_source=receipt, **options
    )
    pointer, output = tmp_path / "pointer.json", tmp_path / "checkpoint.zip"
    context = replace(CONTEXT, run_id=102)
    profile = {**PROFILE, "cloud_syntax_cache": True}
    write_pointer(pointer, context, options["runs_dir"], options["run_id"], profile)
    export_checkpoint(pointer, output, context)
    shutil.move(options["runs_dir"] / options["run_id"], tmp_path / "old-run")
    store.path.unlink()
    restore_checkpoint(
        output.read_bytes(),
        options["runs_dir"],
        replace(CONTEXT, run_id=103),
        {**ORIGIN, "run_id": 102},
        repository,
        first["run_manifest"],
        profile,
        options["source"],
    )
    empty_store = IncrementalStore(store.path.parent)
    monkeypatch.setattr(CheckRunner, "_run_version", lambda *a: pytest.fail("must replay receipt"))
    model = SyntaxReviewModel()
    resumed = review(
        repository,
        model,
        resume=True,
        syntax_cache=empty_store,
        syntax_cache_source={"status": "changed"},
        **options,
    )
    assert model.calls == 0
    assert resumed["syntax_cache"] == first["syntax_cache"]
    assert resumed["budget_usage"] == first["budget_usage"]
    assert resumed["run_manifest"]["syntax_cache"] == cache_identity(store.path.parent)


def test_ci_wires_cache_without_enabling_incremental_or_recovery(repository, tmp_path, monkeypatch):
    source = source_for(repository)
    event = tmp_path / "event.json"
    event.write_text(
        json.dumps(
            {"repository": {"id": 42, "full_name": "example/demo"}, "inputs": {"pr_number": "12"}}
        )
    )
    monkeypatch.setattr("pr_review_harness.cli.CloudContext.from_environment", lambda *a: CONTEXT)
    monkeypatch.setattr("pr_review_harness.cli.configured_client", lambda: object())
    monkeypatch.setattr("pr_review_harness.cli.fetch_snapshot", lambda *a: (repository, source))
    model = SyntaxReviewModel()
    monkeypatch.setattr("pr_review_harness.cli._live_model", lambda *a: model)
    monkeypatch.setattr(
        "pr_review_harness.cli.restore_latest_syntax_cache",
        lambda *a: {"status": "unavailable", "entries": 0},
    )
    monkeypatch.setattr(
        "pr_review_harness.cli.publish_report", lambda *a, **k: {"status": "preview"}
    )
    args = parser().parse_args(
        [
            "github-ci",
            "--event-file",
            str(event),
            "--repository",
            "example/demo",
            "--event-name",
            "workflow_dispatch",
            "--cloud-syntax-cache",
            "--syntax-cache-dir",
            str(tmp_path / "cache"),
            "--memory-db",
            str(tmp_path / "memory.db"),
            "--out",
            str(tmp_path / "ci"),
            "--runs-dir",
            str(tmp_path / "runs"),
        ]
    )
    assert _dispatch(args) == 0 and model.calls == 2
    report = json.loads((args.out / "review.json").read_text())
    assert report["syntax_cache"]["source"]["status"] == "unavailable"
    assert report["syntax_cache"]["misses"] == 3
    assert report["incremental"]["enabled"] is False
    assert not (args.out / "cloud-run.json").exists()
    exported = json.loads((args.out / "syntax-cache-export.json").read_text())
    assert exported["status"] == "saved" and exported["entries"] == 3
    assert RunStore(args.runs_dir, report["run_id"]).load()[0]["source"] == source
