"""PR version binding, publication reconciliation and cross-clone feedback identity."""

import json
import subprocess
from copy import deepcopy
from pathlib import Path

import pytest
from test_runtime import make_snapshot

from pr_review_harness.cli import _dispatch, parser
from pr_review_harness.github import GitHubClient, PRRef, publication_plan, publish_report
from pr_review_harness.persistence import RunStore
from pr_review_harness.runtime import DemoChatModel, review
from pr_review_harness.snapshot import Snapshot


class FakeGitHub(GitHubClient):
    def __init__(self, snapshot):
        super().__init__("test-token")
        self.raw = {
            "number": 12,
            "state": "open",
            "title": "Discount regression",
            "base": {"sha": snapshot.base_sha, "repo": {"id": 42, "full_name": "example/demo"}},
            "head": {"sha": snapshot.head_sha},
        }
        self.reviews = []
        self.posts = []
        self.lost_response = False

    def request(self, method, endpoint, payload=None):
        if method == "GET" and endpoint == "/repos/example/demo/pulls/12":
            return deepcopy(self.raw)
        if endpoint == "/user":
            return {"id": 7}
        if method == "GET" and endpoint.startswith("/repos/example/demo/pulls/12/reviews?"):
            return deepcopy(self.reviews)
        assert method == "POST" and endpoint == "/repos/example/demo/pulls/12/reviews"
        self.posts.append(payload)
        result = {
            "id": 101,
            "html_url": "https://github.com/example/demo/pull/12#review-101",
            "body": payload["body"],
            "commit_id": payload["commit_id"],
            "user": {"id": 7},
            "state": "COMMENTED",
        }
        self.reviews.append(result)
        if self.lost_response:
            raise RuntimeError("remote committed, response lost")
        return result


@pytest.fixture
def remote_report(tmp_path):
    local = make_snapshot(tmp_path)
    snapshot = Snapshot.load(
        local.repo, local.base_sha, local.head_sha, repository_identity="github:42"
    )
    client = FakeGitHub(snapshot)
    source = client.pull_request(PRRef.parse("https://github.com/example/demo/pull/12"))
    report = review(snapshot, DemoChatModel(), run_tests=True, source=source)
    return report, client


@pytest.mark.parametrize(
    "url",
    [
        "http://github.com/a/b/pull/1",
        "https://evil.example/a/b/pull/1",
        "https://github.com@evil.example/a/b/pull/1",
        "https://github.com/../b/pull/1",
        "https://github.com/a/b/pull/0",
        "https://github.com/a/b/pull/1?token=secret",
    ],
)
def test_pr_url_rejects_other_hosts_and_ambiguous_paths(url):
    with pytest.raises(ValueError):
        PRRef.parse(url)


def test_preview_checks_locations_and_makes_no_writes(remote_report, tmp_path):
    report, client = remote_report
    result = publish_report(report, client, tmp_path / "publications")
    assert result["status"] == "preview"
    assert result["payload"]["event"] == "COMMENT"
    assert result["payload"]["commit_id"] == report["head_sha"]
    comment = result["payload"]["comments"][0]
    assert (comment["path"], comment["line"], comment["side"]) == ("pricing.py", 5, "RIGHT")
    assert not client.posts
    assert not (tmp_path / "publications").exists()


@pytest.mark.parametrize("change", ["head", "base", "closed", "bad-line", "failed", "scripted"])
def test_invalid_or_stale_review_never_posts(remote_report, tmp_path, change):
    report, client = remote_report
    if change in {"head", "base"}:
        client.raw[change]["sha"] = "a" * 40
    elif change == "closed":
        client.raw["state"] = "closed"
    elif change == "bad-line":
        report["findings"][0]["line"] = 1
    elif change == "failed":
        report["submission"]["outcome"] = "not_submitted"
    else:
        report["mode"] = "scripted-demo"
    with pytest.raises(ValueError):
        publish_report(report, client, tmp_path / "publications", send=True)
    assert not client.posts


def test_repeat_and_lost_receipt_reconcile_without_duplicate_posts(remote_report, tmp_path):
    report, client = remote_report
    root = tmp_path / "publications"
    first = publish_report(report, client, root, send=True)
    assert first["status"] == "published"
    again = publish_report(report, client, root, send=True)
    assert again["status"] == "already_published"
    # A different local machine has no receipt; remote marker still prevents reposting.
    other = publish_report(report, client, tmp_path / "other-machine", send=True)
    assert other["status"] == "already_published"
    assert len(client.posts) == 1


def test_lost_response_is_reconciled_against_github(remote_report, tmp_path):
    report, client = remote_report
    client.lost_response = True
    root = tmp_path / "publications"
    with pytest.raises(RuntimeError):
        publish_report(report, client, root, send=True)
    result = publish_report(report, client, root, send=True)
    assert result["status"] == "already_published"
    assert len(client.posts) == 1


def test_unknown_publication_without_remote_match_requires_explicit_retry(remote_report, tmp_path):
    report, client = remote_report
    plan = publication_plan(report, client)
    root = tmp_path / "publications"
    RunStore(root, plan["key"]).start("publication", "github_review")
    with pytest.raises(ValueError, match="execution_unknown"):
        publish_report(report, client, root, send=True)
    assert not client.posts
    assert (
        publish_report(report, client, root, send=True, retry_unknown=True)["status"] == "published"
    )


def test_other_user_cannot_spoof_publication_marker(remote_report, tmp_path):
    report, client = remote_report
    plan = publication_plan(report, client)
    client.reviews.append(
        {
            "body": plan["marker"],
            "user": {"id": 999},
            "commit_id": report["head_sha"],
            "state": "COMMENTED",
        }
    )
    assert (
        publish_report(report, client, tmp_path / "publications", send=True)["status"]
        == "published"
    )
    assert len(client.posts) == 1


def test_head_update_during_duplicate_lookup_is_caught_before_post(remote_report, tmp_path):
    report, client = remote_report
    original = client.pages

    def update_during_lookup(endpoint):
        result = original(endpoint)
        client.raw["head"]["sha"] = "b" * 40
        return result

    client.pages = update_during_lookup
    with pytest.raises(ValueError, match="base/head changed"):
        publish_report(report, client, tmp_path / "publications", send=True)
    assert not client.posts


def test_remote_repository_id_is_stable_across_clones(tmp_path):
    local = make_snapshot(tmp_path)
    clone = tmp_path / "clone"
    subprocess.run(["git", "clone", "-q", str(local.repo), str(clone)], check=True)
    one = Snapshot.load(local.repo, local.base_sha, local.head_sha, repository_identity="github:42")
    two = Snapshot.load(clone, local.base_sha, local.head_sha, repository_identity="github:42")
    assert one.repo_id == two.repo_id
    assert Snapshot.load(clone, local.base_sha, local.head_sha).repo_id != local.repo_id


def test_cli_remote_input_is_read_only_and_publication_defaults_to_preview():
    args = parser().parse_args(["github-review", "--pr", "https://github.com/a/b/pull/1"])
    assert not args.run_tests
    publish = parser().parse_args(["github-publish", "--report", "review.json"])
    assert not publish.send


def test_duplicate_findings_are_filtered_in_publication(remote_report):
    report, client = remote_report
    report["findings"].append(deepcopy(report["findings"][0]))
    assert len(publication_plan(report, client)["payload"]["comments"]) == 1


def test_git_transport_does_not_pass_model_keys_or_token_in_arguments(tmp_path, monkeypatch):
    from types import SimpleNamespace

    from pr_review_harness.github import _git

    monkeypatch.setenv("HARNESS_API_KEY", "private-model-key")
    captured = []
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda command, **kwargs: (
            captured.append((command, kwargs)) or SimpleNamespace(returncode=0)
        ),
    )
    _git(tmp_path, "private-github-token", "init", "--template=")
    command, options = captured[0]
    assert "private-github-token" not in json.dumps(command)
    assert "HARNESS_API_KEY" not in options["env"]
    assert "http.https://github.com/.extraheader" == options["env"]["GIT_CONFIG_KEY_0"]


def test_duplicate_lookup_reads_all_pages_and_refuses_unbounded_lists():
    client = GitHubClient()
    pages = []

    def request(method, endpoint, payload=None):
        pages.append(endpoint)
        return [{}] * 100 if endpoint.endswith("&page=1") else [{"id": 101}]

    client.request = request
    assert len(client.pages("/reviews")) == 101
    assert len(pages) == 2
    client.request = lambda *a: [{}] * 100
    with pytest.raises(ValueError, match="Too many"):
        client.pages("/reviews")


def test_cli_review_fetch_and_preview_flow(remote_report, tmp_path, monkeypatch):
    report, client = remote_report
    from pr_review_harness import cli

    snapshot = Snapshot.load(
        Path(report["repo"]),
        report["base_sha"],
        report["head_sha"],
        repository_identity="github:42",
    )
    monkeypatch.setattr(cli, "configured_client", lambda: client)
    monkeypatch.setattr(cli, "fetch_snapshot", lambda *a: (snapshot, report["source"]))
    monkeypatch.setattr(cli, "_live_model", lambda *a: EmptyReviewModel())
    root = tmp_path / "flow"
    common = ["--pr", "https://github.com/example/demo/pull/12", "--out", str(root)]
    assert _dispatch(parser().parse_args(["github-fetch", *common])) == 0
    assert json.loads((root / "snapshot.json").read_text())["repo_id"] == snapshot.repo_id
    assert (
        _dispatch(
            parser().parse_args(
                [
                    "github-review",
                    *common,
                    "--runs-dir",
                    str(root / "runs"),
                    "--memory-db",
                    str(root / "memory.sqlite3"),
                ]
            )
        )
        == 0
    )
    generated = json.loads((root / "review.json").read_text())
    assert generated["source"] == report["source"]
    assert (
        _dispatch(
            parser().parse_args(
                [
                    "github-publish",
                    "--report",
                    str(root / "review.json"),
                    "--out",
                    str(root / "preview"),
                ]
            )
        )
        == 0
    )
    assert not client.posts


def test_remote_feedback_is_stored_under_same_identity_as_review(
    remote_report, tmp_path, monkeypatch
):
    from pr_review_harness import cli
    from pr_review_harness.memory import MemoryStore

    report, client = remote_report
    snapshot = Snapshot.load(
        Path(report["repo"]),
        report["base_sha"],
        report["head_sha"],
        repository_identity="github:42",
    )
    monkeypatch.setattr(cli, "configured_client", lambda: client)
    monkeypatch.setattr(cli, "fetch_snapshot", lambda *a: (snapshot, report["source"]))
    path = tmp_path / "remote-review.json"
    path.write_text(json.dumps(report))
    db = tmp_path / "memory.sqlite3"
    assert (
        _dispatch(
            parser().parse_args(
                [
                    "memory",
                    "feedback",
                    "--pr",
                    "https://github.com/example/demo/pull/12",
                    "--report",
                    str(path),
                    "--memory-db",
                    str(db),
                    "--text",
                    "Confirmed fractional discount",
                    "--source",
                    "maintainer:12",
                    "--disposition",
                    "accepted",
                    "--scope",
                    "pricing.py",
                ]
            )
        )
        == 0
    )
    recalled = MemoryStore(db).recall_snapshot(snapshot.repo_id, ["pricing.py"])
    assert "Confirmed fractional discount" in recalled.text
    assert not client.posts


class EmptyReviewModel(DemoChatModel):
    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        from langchain_core.messages import AIMessage
        from langchain_core.outputs import ChatGeneration, ChatResult

        return ChatResult(
            generations=[
                ChatGeneration(
                    message=AIMessage(
                        content="",
                        tool_calls=[
                            {
                                "name": "submit_review",
                                "args": {"findings": []},
                                "id": "empty-review",
                            }
                        ],
                    )
                )
            ]
        )
