"""Independent stores, feedback lifecycle and trusted GitHub memory loading."""

import base64
import hashlib
import json
import sqlite3
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from pr_review_harness.memory import MemoryStore
from pr_review_harness.memory_bundle import (
    MAX_BUNDLE_BYTES,
    MAX_BUNDLE_RECORDS,
    bundle_hash,
    decode_bundle,
    load_github_memory,
)

REPO_ID = hashlib.sha256(b"github:42").hexdigest()


def seeded_store(path):
    store = MemoryStore(path)
    old = store.add(REPO_ID, "Original guidance", "Maintainer PR 1", path_glob="src/**/*.py")
    new = store.revise(REPO_ID, old, "Revised guidance", "Maintainer PR 2", reason="Clarified")
    dismissed = store.add(
        REPO_ID, "Rejected suggestion", "Maintainer PR 3", disposition="dismissed"
    )
    store.revoke(REPO_ID, dismissed, reason="No longer applicable")
    expired = store.add(REPO_ID, "Expired guidance", "Maintainer PR 4")
    with sqlite3.connect(path) as connection:
        created = datetime.now(UTC) - timedelta(days=2)
        connection.execute(
            "UPDATE review_memory SET created_at=?, expires_at=? WHERE id=?",
            (created.isoformat(), (created + timedelta(days=1)).isoformat(), expired),
        )
    return store, new


def test_round_trip_keeps_history_expiry_and_stable_uids_across_stores(tmp_path):
    original, new = seeded_store(tmp_path / "original.db")
    bundle = original.export_bundle(REPO_ID)
    target = MemoryStore(tmp_path / "target.db")
    target.add("unrelated", "Other repository", "Other maintainer")
    target.import_bundle(REPO_ID, decode_bundle(json.dumps(bundle).encode(), REPO_ID))
    assert target.export_bundle(REPO_ID) == bundle
    assert len(target.list_records(REPO_ID)) == 4
    assert target.list_records("unrelated")[0]["text"] == "Other repository"
    recalled = target.recall_snapshot(REPO_ID, ["src/nested/api.py"])
    assert "Revised guidance" in recalled.text
    assert "Original guidance" not in recalled.text
    assert "Expired guidance" not in recalled.text
    assert "Rejected suggestion" not in recalled.text
    uid = next(r["record_uid"] for r in original.list_records(REPO_ID) if r["id"] == new)
    assert recalled.manifest["records"][0]["uid"] == uid
    assert target.list_records(REPO_ID)[0]["id"] != original.list_records(REPO_ID)[0]["id"]


def test_refresh_revokes_future_recall_and_keeps_prior_snapshot_frozen(tmp_path):
    origin = MemoryStore(tmp_path / "origin.db")
    record = origin.add(REPO_ID, "Confirmed rule", "Maintainer")
    target = MemoryStore(tmp_path / "target.db")
    bundle = origin.export_bundle(REPO_ID)
    target.import_bundle(REPO_ID, bundle, origin={"commit_sha": "a" * 40})
    first = target.recall_snapshot(REPO_ID, ["src/a.py"])
    target.import_bundle(REPO_ID, bundle, origin={"commit_sha": "b" * 40})
    assert len(target.list_records(REPO_ID)) == 1
    origin.revoke(REPO_ID, record, reason="Maintainer revoked")
    target.import_bundle(REPO_ID, origin.export_bundle(REPO_ID))
    assert target.recall(REPO_ID, ["src/a.py"]) == ""
    assert "Confirmed rule" in first.text
    assert first.manifest["storage"]["origin"]["commit_sha"] == "a" * 40
    with pytest.raises(ValueError, match="terminal"):
        target.import_bundle(REPO_ID, bundle)
    assert target.recall(REPO_ID, ["src/a.py"]) == ""


def test_refresh_refuses_unsaved_edits_but_accepts_exact_published_state(tmp_path):
    origin = MemoryStore(tmp_path / "origin.db")
    origin.add(REPO_ID, "Shared rule", "Maintainer")
    target = MemoryStore(tmp_path / "target.db")
    bundle = origin.export_bundle(REPO_ID)
    target.import_bundle(REPO_ID, bundle)
    target.add(REPO_ID, "Unsaved local rule", "Local maintainer")
    assert target.recall_snapshot(REPO_ID, []).manifest["storage"]["local_changes"]
    with pytest.raises(ValueError, match="Local feedback changed"):
        target.import_bundle(REPO_ID, bundle)
    assert "Unsaved local rule" in target.recall(REPO_ID, ["src/a.py"])
    published = target.export_bundle(REPO_ID)
    target.import_bundle(REPO_ID, published, origin={"commit_sha": "c" * 40})
    storage = target.recall_snapshot(REPO_ID, []).manifest["storage"]
    assert storage["bundle_sha256"] == bundle_hash(published)
    assert not storage["local_changes"]


def test_import_refuses_existing_unrelated_feedback_and_uid_collision(tmp_path):
    original = MemoryStore(tmp_path / "origin.db")
    original.add(REPO_ID, "Remote", "Maintainer")
    target = MemoryStore(tmp_path / "target.db")
    target.add(REPO_ID, "Local", "Maintainer")
    with pytest.raises(ValueError, match="empty repository namespace"):
        target.import_bundle(REPO_ID, original.export_bundle(REPO_ID))
    assert target.list_records(REPO_ID)[0]["text"] == "Local"
    target.import_bundle(REPO_ID, target.export_bundle(REPO_ID))
    bundle = deepcopy(original.export_bundle(REPO_ID))
    bundle["repo_id"] = "other"
    original.import_bundle(REPO_ID, original.export_bundle(REPO_ID))
    with pytest.raises(ValueError, match="another repository"):
        original.import_bundle("other", bundle)
    assert original.list_records("other") == []


@pytest.mark.parametrize(
    "mutation",
    [
        "repo",
        "version",
        "duplicate_uid",
        "missing_replacement",
        "unknown_field",
        "oversize_text",
        "bad_timestamp",
        "naive_timestamp",
        "path_traversal",
        "missing_reason",
        "bad_disposition",
        "bad_status",
        "too_many",
    ],
)
def test_invalid_bundle_is_rejected_before_database_changes(tmp_path, mutation):
    origin, _ = seeded_store(tmp_path / "origin.db")
    bundle = origin.export_bundle(REPO_ID)
    record = next(r for r in bundle["records"] if r["status"] == "active")
    if mutation == "repo":
        bundle["repo_id"] = "wrong"
    elif mutation == "version":
        bundle["schema_version"] = True
    elif mutation == "duplicate_uid":
        bundle["records"].append(deepcopy(record))
    elif mutation == "missing_replacement":
        next(r for r in bundle["records"] if r["status"] == "superseded")["replaced_by"] = "0" * 32
    elif mutation == "unknown_field":
        record["automatic_instructions"] = "execute this"
    elif mutation == "oversize_text":
        record["text"] = "a" * 8001
    elif mutation == "bad_timestamp":
        record["expires_at"] = "not a date"
    elif mutation == "naive_timestamp":
        record["created_at"] = datetime.now().isoformat()
    elif mutation == "path_traversal":
        record["path_glob"] = "../*.py"
    elif mutation == "missing_reason":
        next(r for r in bundle["records"] if r["status"] == "revoked")["status_reason"] = None
    elif mutation == "bad_disposition":
        record["disposition"] = []
    elif mutation == "bad_status":
        record["status"] = []
    else:
        bundle["records"] = [record] * (MAX_BUNDLE_RECORDS + 1)
    target = MemoryStore(tmp_path / "target.db")
    with pytest.raises(ValueError):
        target.import_bundle(REPO_ID, bundle)
    assert target.list_records(REPO_ID) == []


def test_refresh_cannot_drop_history_or_mutate_original_text(tmp_path):
    origin = MemoryStore(tmp_path / "origin.db")
    origin.add(REPO_ID, "Shared", "Maintainer")
    bundle = origin.export_bundle(REPO_ID)
    target = MemoryStore(tmp_path / "target.db")
    target.import_bundle(REPO_ID, bundle)
    with pytest.raises(ValueError, match="retain"):
        target.import_bundle(REPO_ID, {**bundle, "records": []})
    changed = deepcopy(bundle)
    changed["records"][0]["text"] = "Changed in place"
    with pytest.raises(ValueError, match="immutable"):
        target.import_bundle(REPO_ID, changed)
    assert target.export_bundle(REPO_ID) == bundle


def test_decoder_rejects_duplicate_keys_size_and_invalid_encoding():
    with pytest.raises(ValueError, match="duplicate keys"):
        decode_bundle(b'{"schema_version":1,"schema_version":2}', REPO_ID)
    with pytest.raises(ValueError, match="size"):
        decode_bundle(b" " * (MAX_BUNDLE_BYTES + 1), REPO_ID)
    with pytest.raises(ValueError, match="UTF-8"):
        decode_bundle(b"\xff", REPO_ID)


def test_legacy_store_assigns_stable_uid_once(tmp_path):
    path = tmp_path / "legacy.db"
    with sqlite3.connect(path) as db:
        db.execute(
            "CREATE TABLE review_memory (id INTEGER PRIMARY KEY AUTOINCREMENT, repo_id TEXT, "
            "text TEXT,source TEXT,path_glob TEXT,disposition TEXT,created_at TEXT,expires_at TEXT)"
        )
        now = datetime.now(UTC)
        db.execute(
            "INSERT INTO review_memory VALUES (1,?,?,?,?,?,?,?)",
            (
                REPO_ID,
                "Legacy",
                "Maintainer",
                "*",
                "accepted",
                now.isoformat(),
                (now + timedelta(days=90)).isoformat(),
            ),
        )
    first = MemoryStore(path).export_bundle(REPO_ID)
    assert MemoryStore(path).export_bundle(REPO_ID) == first


class MemoryClient:
    def __init__(self, bundle):
        self.calls = []
        self.content = json.dumps(bundle).encode()
        self.commit = "c" * 40
        self.value = {
            "type": "file",
            "path": ".harness/feedback.json",
            "encoding": "base64",
            "size": len(self.content),
            "content": base64.b64encode(self.content).decode(),
            "sha": hashlib.sha1(f"blob {len(self.content)}\0".encode() + self.content).hexdigest(),
        }

    def request(self, method, endpoint):
        self.calls.append((method, endpoint))
        assert method == "GET"
        if endpoint == "/repos/example/demo":
            return {"id": 42, "full_name": "example/demo", "default_branch": "main"}
        if endpoint == "/repos/example/demo/commits/main":
            return {"sha": self.commit}
        assert endpoint == f"/repos/example/demo/contents/.harness/feedback.json?ref={self.commit}"
        return self.value


def github_source():
    return {"owner": "example", "repository": "demo", "repository_id": 42}


def test_github_memory_pins_default_branch_and_records_exact_file_provenance(tmp_path):
    origin = MemoryStore(tmp_path / "origin.db")
    origin.add(REPO_ID, "Confirmed", "Maintainer", path_glob="src/**/*.py")
    client = MemoryClient(origin.export_bundle(REPO_ID))
    snapshot = SimpleNamespace(repo_id=REPO_ID, repository_identity="github:42", head_sha="b" * 40)
    target = MemoryStore(tmp_path / "target.db")
    receipt = load_github_memory(snapshot, github_source(), client, target)
    assert receipt["commit_sha"] == "c" * 40 and receipt["commit_sha"] != snapshot.head_sha
    assert len(client.calls) == 3
    assert receipt["file_sha256"] == hashlib.sha256(client.content).hexdigest()
    recalled = target.recall_snapshot(REPO_ID, ["src/nested/a.py"])
    assert recalled.manifest["storage"]["origin"]["commit_sha"] == receipt["commit_sha"]
    assert (
        recalled.manifest["records"][0]["uid"] == origin.export_bundle(REPO_ID)["records"][0]["uid"]
    )
    assert target.recall(REPO_ID, ["tests/a.py"]) == ""


@pytest.mark.parametrize("change", ["identity", "blob", "size", "encoding", "schema", "missing"])
def test_invalid_or_missing_remote_memory_does_not_install_partial_feedback(tmp_path, change):
    origin = MemoryStore(tmp_path / "origin.db")
    origin.add(REPO_ID, "Confirmed", "Maintainer")
    client = MemoryClient(origin.export_bundle(REPO_ID))
    if change == "blob":
        client.value["sha"] = "f" * 40
    elif change == "size":
        client.value["size"] += 1
    elif change == "encoding":
        client.value["encoding"] = "none"
    elif change == "schema":
        client.value["type"] = "dir"
    elif change == "missing":

        def missing(method, endpoint):
            raise RuntimeError("GitHub API HTTP 404")

        client.request = missing
    snapshot = SimpleNamespace(
        repo_id=REPO_ID, repository_identity="github:43" if change == "identity" else "github:42"
    )
    target = MemoryStore(tmp_path / "target.db")
    with pytest.raises((ValueError, RuntimeError)):
        load_github_memory(snapshot, github_source(), client, target)
    assert target.list_records(REPO_ID) == []


def test_public_export_import_commands_preserve_curation_history(tmp_path):
    from pr_review_harness.cli import _dispatch, parser
    from pr_review_harness.demo import create_demo
    from pr_review_harness.snapshot import Snapshot

    repo = tmp_path / "repo"
    create_demo(repo)
    repo_id = Snapshot.load(repo, "HEAD", "HEAD").repo_id
    source = MemoryStore(tmp_path / "source.db")
    source.add(repo_id, "Confirmed guidance", "Maintainer", rule_key="boundary.empty")
    bundle_path = tmp_path / "feedback.json"
    assert (
        _dispatch(
            parser().parse_args(
                [
                    "memory",
                    "export",
                    "--repo",
                    str(repo),
                    "--memory-db",
                    str(source.db_path),
                    "--out",
                    str(bundle_path),
                ]
            )
        )
        == 0
    )
    imported = tmp_path / "imported.db"
    assert (
        _dispatch(
            parser().parse_args(
                [
                    "memory",
                    "import",
                    "--repo",
                    str(repo),
                    "--memory-db",
                    str(imported),
                    "--bundle",
                    str(bundle_path),
                ]
            )
        )
        == 0
    )
    assert MemoryStore(imported).export_bundle(repo_id) == source.export_bundle(repo_id)
    assert (
        _dispatch(
            parser().parse_args(
                [
                    "memory",
                    "revise",
                    "--repo",
                    str(repo),
                    "--memory-db",
                    str(imported),
                    "--id",
                    "1",
                    "--text",
                    "Revised guidance",
                    "--source",
                    "Maintainer clarification",
                    "--reason",
                    "Updated contract",
                ]
            )
        )
        == 0
    )
    assert "Revised guidance" in MemoryStore(imported).recall(repo_id, [])
    assert "Confirmed guidance" not in MemoryStore(imported).recall(repo_id, [])


@pytest.mark.parametrize("valid", [True, False])
def test_ci_loads_feedback_before_model_and_saves_its_source(tmp_path, monkeypatch, valid):
    from langchain_core.messages import AIMessage
    from langchain_core.outputs import ChatGeneration, ChatResult

    from pr_review_harness.cli import _dispatch, parser
    from pr_review_harness.demo import create_demo
    from pr_review_harness.runtime import DemoChatModel
    from pr_review_harness.snapshot import Snapshot

    class FeedbackAwareModel(DemoChatModel):
        def _generate(self, messages, stop=None, run_manager=None, **kwargs):
            assert "Confirmed cloud guidance" in str(messages)
            return ChatResult(
                generations=[
                    ChatGeneration(
                        message=AIMessage(
                            content="",
                            tool_calls=[
                                {
                                    "name": "submit_review",
                                    "args": {"findings": []},
                                    "id": "cloud-submit",
                                }
                            ],
                        )
                    )
                ]
            )

    repo = tmp_path / "repo"
    base, head = create_demo(repo)
    snapshot = Snapshot.load(repo, base, head, repository_identity="github:42")
    source = {
        **github_source(),
        "kind": "github",
        "url": "https://github.com/example/demo/pull/12",
        "base_sha": base,
        "head_sha": head,
        "state": "open",
        "number": 12,
    }
    origin = MemoryStore(tmp_path / "origin.db")
    origin.add(REPO_ID, "Confirmed cloud guidance", "Maintainer", path_glob="pricing.py")
    client = MemoryClient(origin.export_bundle(REPO_ID))
    if not valid:
        client.value["sha"] = "f" * 40
    event_path = tmp_path / "event.json"
    event_path.write_text(
        json.dumps(
            {
                "repository": {"id": 42, "full_name": "example/demo"},
                "inputs": {"pr_number": "12"},
            }
        )
    )
    out = tmp_path / "out"
    args = parser().parse_args(
        [
            "github-ci",
            "--repository",
            "example/demo",
            "--event-file",
            str(event_path),
            "--event-name",
            "workflow_dispatch",
            "--github-memory",
            "--out",
            str(out),
            "--runs-dir",
            str(tmp_path / "runs"),
            "--memory-db",
            str(tmp_path / "cloud.db"),
        ]
    )
    monkeypatch.setattr("pr_review_harness.cli.configured_client", lambda: client)
    monkeypatch.setattr("pr_review_harness.cli.fetch_snapshot", lambda *a: (snapshot, source))
    attempts = []

    def model(args):
        attempts.append("model")
        return FeedbackAwareModel()

    def publish(report, client, root, *, send):
        assert not send
        attempts.append("preview")
        return {"status": "preview"}

    monkeypatch.setattr("pr_review_harness.cli._live_model", model)
    monkeypatch.setattr("pr_review_harness.cli.publish_report", publish)
    if not valid:
        with pytest.raises(ValueError, match="blob SHA"):
            _dispatch(args)
        assert attempts == []
        assert not (out / "review.json").exists()
        return
    assert _dispatch(args) == 0
    assert attempts == ["model", "preview"]
    report = json.loads((out / "review.json").read_text())
    recalled = report["context"]["memory_snapshot"]
    assert "Confirmed cloud guidance" in recalled["text"]
    assert recalled["storage"]["origin"]["commit_sha"] == client.commit
    assert recalled["records"][0]["uid"] == origin.export_bundle(REPO_ID)["records"][0]["uid"]
    assert json.loads((out / "memory-source.json").read_text())["record_count"] == 1
