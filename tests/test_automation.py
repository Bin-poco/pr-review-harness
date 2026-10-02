"""Event spoofing, outdated versions and the complete offline CI dispatch path."""

import json
from copy import deepcopy
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path

import pytest
from langchain_core.messages import AIMessage
from langchain_core.outputs import ChatGeneration, ChatResult

from pr_review_harness.automation import parse_event
from pr_review_harness.cli import _dispatch, parser
from pr_review_harness.demo import create_demo
from pr_review_harness.runtime import DemoChatModel
from pr_review_harness.snapshot import Snapshot


class EmptyReviewModel(DemoChatModel):
    """A syntax-only CI plumbing fixture; no claim about defect detection."""

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        return ChatResult(
            generations=[
                ChatGeneration(
                    message=AIMessage(
                        content="",
                        tool_calls=[
                            {
                                "name": "submit_review",
                                "args": {"findings": []},
                                "id": "ci-submit",
                            }
                        ],
                    )
                )
            ]
        )


def pr_event(base="a" * 40, head="b" * 40):
    repo = {"id": 42, "full_name": "example/demo"}
    return {
        "repository": repo,
        "action": "synchronize",
        "number": 12,
        "pull_request": {
            "number": 12,
            "state": "open",
            "draft": False,
            "base": {"repo": repo, "sha": base},
            "head": {"sha": head},
            "title": "$(touch /tmp/should-not-execute)",
        },
    }


def test_event_binds_base_repository_and_both_versions():
    event = parse_event(pr_event(), repository="example/demo", event_name="pull_request_target")
    assert event.ref.url == "https://github.com/example/demo/pull/12"
    source = {"repository_id": 42, "base_sha": "a" * 40, "head_sha": "b" * 40, "state": "open"}
    event.validate_source(source)
    for key, new in (
        ("repository_id", 43),
        ("base_sha", "c" * 40),
        ("head_sha", "c" * 40),
        ("state", "closed"),
    ):
        with pytest.raises(ValueError):
            event.validate_source({**source, key: new})


@pytest.mark.parametrize("change", ["repo", "base_repo", "sha", "number", "event_name"])
def test_spoofed_or_malformed_event_fails_before_review(change):
    payload = deepcopy(pr_event())
    name = "pull_request_target"
    if change == "repo":
        payload["repository"]["full_name"] = "attacker/other"
    elif change == "base_repo":
        payload["pull_request"]["base"]["repo"] = {"id": 99, "full_name": "example/demo"}
    elif change == "sha":
        payload["pull_request"]["head"]["sha"] = "--exec"
    elif change == "number":
        payload["number"] = True
    else:
        name = "pull_request"
    with pytest.raises(ValueError):
        parse_event(payload, repository="example/demo", event_name=name)


def test_draft_event_skips_without_model_or_network(tmp_path, monkeypatch):
    payload = pr_event()
    payload["pull_request"]["draft"] = True
    event_path = tmp_path / "event.json"
    event_path.write_text(json.dumps(payload))
    args = parser().parse_args(
        [
            "github-ci",
            "--event-file",
            str(event_path),
            "--repository",
            "example/demo",
            "--event-name",
            "pull_request_target",
            "--out",
            str(tmp_path / "out"),
        ]
    )

    def unexpected(*a, **k):
        raise AssertionError("A skipped event should not contact model or GitHub")

    monkeypatch.setattr("pr_review_harness.cli.configured_client", unexpected)
    monkeypatch.setattr("pr_review_harness.cli._live_model", unexpected)
    assert _dispatch(args) == 0
    assert json.loads((args.out / "automation.json").read_text())["reason"] == "closed_or_draft"


def test_manual_dispatch_accepts_only_number_in_configured_repository():
    payload = {"repository": pr_event()["repository"], "inputs": {"pr_number": "12"}}
    event = parse_event(payload, repository="example/demo", event_name="workflow_dispatch")
    assert event.ref.number == 12 and event.head_sha is None
    for number in ("https://evil.test/a/pull/12", "-1", "0", "1\nmalicious"):
        payload["inputs"]["pr_number"] = number
        with pytest.raises(ValueError):
            parse_event(payload, repository="example/demo", event_name="workflow_dispatch")


def test_credential_bearing_ci_rejects_repository_execution(tmp_path):
    args = parser().parse_args(
        [
            "github-ci",
            "--event-file",
            str(tmp_path / "does-not-exist"),
            "--repository",
            "example/demo",
            "--run-tests",
        ]
    )
    with pytest.raises(ValueError, match="syntax checks only"):
        _dispatch(args)


@pytest.mark.parametrize("send", [False, True])
def test_ci_dispatch_runs_review_and_preserves_preview_or_send(tmp_path, monkeypatch, send):
    repo = tmp_path / "repo"
    base, head = create_demo(repo)
    snapshot = Snapshot.load(repo, base, head, repository_identity="github:42")
    event_path = tmp_path / "event.json"
    event_path.write_text(json.dumps(pr_event(base, head)))
    args = parser().parse_args(
        [
            "github-ci",
            "--event-file",
            str(event_path),
            "--repository",
            "example/demo",
            "--event-name",
            "pull_request_target",
            "--out",
            str(tmp_path / "out"),
            "--runs-dir",
            str(tmp_path / "runs"),
            "--memory-db",
            str(tmp_path / "memory.db"),
            *(["--send"] if send else []),
        ]
    )
    source = {
        "kind": "github",
        "url": "https://github.com/example/demo/pull/12",
        "repository_id": 42,
        "base_sha": base,
        "head_sha": head,
        "state": "open",
    }
    monkeypatch.setattr("pr_review_harness.cli.configured_client", lambda: object())
    monkeypatch.setattr("pr_review_harness.cli.fetch_snapshot", lambda *a: (snapshot, source))
    monkeypatch.setattr("pr_review_harness.cli._live_model", lambda *a: EmptyReviewModel())
    attempts = []

    def publish(report, client, root, *, send):
        attempts.append(send)
        assert report["source"] == source
        assert report["run_manifest"]["execution"] == {"enabled": False}
        return {"status": "published" if send else "preview"}

    monkeypatch.setattr("pr_review_harness.cli.publish_report", publish)
    assert _dispatch(args) == 0
    assert attempts == [send]
    assert json.loads((args.out / "automation.json").read_text())["status"] == "completed"


def test_outdated_ci_event_never_calls_model_or_publisher(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    base, head = create_demo(repo)
    snapshot = Snapshot.load(repo, base, head)
    event_path = tmp_path / "event.json"
    event_path.write_text(json.dumps(pr_event(base, "c" * 40)))
    args = parser().parse_args(
        [
            "github-ci",
            "--event-file",
            str(event_path),
            "--repository",
            "example/demo",
            "--event-name",
            "pull_request_target",
            "--send",
        ]
    )
    monkeypatch.setattr("pr_review_harness.cli.configured_client", lambda: object())
    monkeypatch.setattr(
        "pr_review_harness.cli.fetch_snapshot",
        lambda *a: (
            snapshot,
            {"repository_id": 42, "base_sha": base, "head_sha": head, "state": "open"},
        ),
    )

    def unexpected(*a, **k):
        raise AssertionError("Old event must not call a model or publish")

    monkeypatch.setattr("pr_review_harness.cli._live_model", unexpected)
    monkeypatch.setattr("pr_review_harness.cli.publish_report", unexpected)
    with pytest.raises(ValueError, match="changed since"):
        _dispatch(args)


def test_business_workflow_generator_requires_trusted_commit():
    path = Path(__file__).parents[1] / "scripts/install_workflow.py"
    spec = spec_from_file_location("workflow_generator", path)
    module = module_from_spec(spec)
    spec.loader.exec_module(module)
    value = module.render("example/harness", "a" * 40)
    assert 'repository: "example/harness"' in value and "ref: '" + "a" * 40 + "'" in value
    assert "ref: ${{ github.sha }}" not in value
    for repo, ref in [("../harness", "a" * 40), ("example/harness", "main")]:
        with pytest.raises(ValueError):
            module.render(repo, ref)
