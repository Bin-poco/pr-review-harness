"""Prevent stale findings, checks, scope or receipts after a PR update."""

import json
from dataclasses import replace

import pytest
from langchain_core.messages import AIMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from pydantic import Field

from pr_review_harness.budget import BudgetPolicy
from pr_review_harness.checks import CheckRunner
from pr_review_harness.context import build_context
from pr_review_harness.demo import _git
from pr_review_harness.incremental import IncrementalStore, configuration_digest, review_key
from pr_review_harness.memory import MemoryStore
from pr_review_harness.persistence import RunStore, identity
from pr_review_harness.report import render_markdown
from pr_review_harness.runtime import DemoChatModel, ReviewFailure, review
from pr_review_harness.snapshot import Snapshot


@pytest.fixture
def repository(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.name", "Harness Tests")
    _git(repo, "config", "user.email", "tests@example.invalid")
    _git(repo, "config", "commit.gpgsign", "false")
    (repo / "pricing.py").write_text("def value():\n    return 1\n")
    (repo / "z_module.py").write_text("def unchanged():\n    return 0\n")
    (repo / "test_value.py").write_text(
        "import unittest\nfrom pricing import value\n"
        "class Tests(unittest.TestCase):\n"
        "    def test_value(self):\n        self.assertEqual(value(), 1)\n"
    )
    base = commit(repo, "base")
    (repo / "pricing.py").write_text("def value(:\n    return 1\n")
    head = commit(repo, "broken syntax")
    return Snapshot.load(repo, base, head)


def commit(repo, message):
    _git(repo, "add", ".")
    _git(repo, "-c", "core.hooksPath=/dev/null", "commit", "-qm", message)
    return _git(repo, "rev-parse", "HEAD")


def update(snapshot, path, content):
    (snapshot.repo / path).write_text(content)
    head = commit(snapshot.repo, f"update {path}")
    return Snapshot.load(snapshot.repo, snapshot.base_sha, head)


class SyntaxReviewModel(DemoChatModel):
    """Preset calls based on fresh evidence; this is a process test, not an LLM eval."""

    calls: int = 0
    fail_submit: bool = False
    contexts: list[str] = Field(default_factory=list)

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        self.calls += 1
        self.contexts.extend(str(m.content) for m in messages if m.type == "human")
        completed = [m for m in messages if m.type == "tool" and m.name == "run_check"]
        if not completed:
            calls = [
                {
                    "name": "run_check",
                    "args": {"kind": "syntax", "path": path},
                    "id": f"check-{index}",
                    "type": "tool_call",
                }
                for index, path in enumerate(("pricing.py", "z_module.py"))
            ]
        else:
            if self.fail_submit:
                raise RuntimeError("interrupted after checks")
            pricing = next(
                json.loads(m.content)
                for m in completed
                if json.loads(m.content).get("path") == "pricing.py"
            )
            findings = []
            if pricing["head"]["status"] == "failed":
                findings = [
                    {
                        "path": "pricing.py",
                        "line": 1,
                        "severity": "P2",
                        "title": "Invalid function signature",
                        "explanation": "Cannot import.",
                        "trigger": "Import pricing",
                        "confidence": "high",
                        "evidence_ids": [pricing["id"]],
                    }
                ]
            calls = [
                {
                    "name": "submit_review",
                    "args": {"findings": findings},
                    "id": "submit",
                    "type": "tool_call",
                }
            ]
        return ChatResult(
            generations=[ChatGeneration(message=AIMessage(content="", tool_calls=calls))]
        )


def options(tmp_path):
    return {
        "incremental": True,
        "incremental_dir": tmp_path / "reuse",
        "runs_dir": tmp_path / "runs",
        "review_key": "local-pr-1",
        "mode": "scripted-demo",
    }


def test_updates_reprioritize_full_scope_and_rebind_cached_evidence(repository, tmp_path):
    first = review(repository, SyntaxReviewModel(), run_id="first", **options(tmp_path))
    assert first["incremental"]["reason"] == "no_completed_baseline"
    assert first["incremental"]["check_cache"] == {
        "eligible_kind": "syntax",
        "hits": 1,
        "misses": 3,
        "unittest_reused": False,
        "scope": "evidence in this review; resumed receipts retain original cache provenance",
    }
    assert len(first["findings"]) == 1
    changed = update(repository, "z_module.py", "def unchanged():\n    return 2\n")
    model = SyntaxReviewModel()
    second = review(changed, model, run_id="second", **options(tmp_path))
    plan = second["incremental"]
    assert plan["mode"] == "incremental"
    assert plan["updated_paths"] == ["z_module.py"]
    assert plan["check_cache"]["hits"] == 3
    assert plan["check_cache"]["misses"] == 1
    diffs = [i["path"] for i in second["context"]["items"] if "PR diff" in i["reason"]]
    assert diffs == ["z_module.py", "pricing.py"]
    assert "Review the full PR" in model.contexts[0]
    assert model.calls > 0
    assert second["findings"][0]["id"] != first["findings"][0]["id"]
    assert second["evidence"][0]["head"]["sha"] == changed.head_sha
    assert second["evidence"][0]["head"]["cache"]["origin_sha"] == repository.head_sha
    assert second["budget_usage"]["tool_attempts"] == first["budget_usage"]["tool_attempts"] == 3
    assert "语法检查缓存" in render_markdown(second)


def test_fix_reexecutes_changed_content_and_does_not_replay_findings(repository, tmp_path):
    review(repository, SyntaxReviewModel(), run_id="broken", **options(tmp_path))
    fixed = update(repository, "pricing.py", "def value():\n    return 2\n")
    result = review(fixed, SyntaxReviewModel(), run_id="fixed", **options(tmp_path))
    assert result["incremental"]["mode"] == "incremental"
    assert result["incremental"]["check_cache"]["misses"] == 1
    assert result["evidence"][0]["head"]["status"] == "passed"
    assert result["findings"] == []


def test_completed_resume_uses_frozen_plan_and_receipts_without_execution(
    repository, tmp_path, monkeypatch
):
    settings = options(tmp_path)
    first = review(repository, SyntaxReviewModel(), run_id="resume", **settings)
    changed = update(repository, "z_module.py", "def unchanged():\n    return 2\n")
    review(changed, SyntaxReviewModel(), run_id="later", **settings)
    # Deliberately remove the optional check cache: the run's receipts are sufficient.
    with IncrementalStore(settings["incremental_dir"]).connect() as conn:
        conn.execute("DELETE FROM syntax_checks")
    monkeypatch.setattr(CheckRunner, "_run_version", lambda *a: pytest.fail("check repeated"))
    model = SyntaxReviewModel()
    resumed = review(repository, model, run_id="resume", resume=True, **settings)
    assert model.calls == 0
    assert resumed["incremental"]["mode"] == first["incremental"]["mode"]
    assert resumed["incremental"]["baseline_update"] == "skipped_concurrent_update"
    assert resumed["evidence"] == first["evidence"]
    assert resumed["findings"] == first["findings"]
    assert resumed["budget_usage"] == first["budget_usage"]
    with pytest.raises(ValueError, match="Cannot resume"):
        review(
            repository,
            SyntaxReviewModel(),
            run_id="resume",
            resume=True,
            **{**settings, "incremental": False},
        )


def test_interruption_never_promotes_a_baseline_and_resume_preserves_plan(repository, tmp_path):
    settings = options(tmp_path)
    with pytest.raises(ReviewFailure) as exc:
        review(repository, SyntaxReviewModel(fail_submit=True), run_id="interrupted", **settings)
    assert exc.value.partial["incremental"]["enabled"]
    store = IncrementalStore(settings["incremental_dir"])
    assert store.baseline(repository.repo_id, "local-pr-1") is None
    result = review(repository, SyntaxReviewModel(), run_id="interrupted", resume=True, **settings)
    assert result["incremental"]["reason"] == "no_completed_baseline"
    assert store.baseline(repository.repo_id, "local-pr-1")["run_id"] == "interrupted"


def test_unchanged_snapshot_calls_fresh_model_and_reuses_pure_checks(
    repository, tmp_path, monkeypatch
):
    review(repository, SyntaxReviewModel(), run_id="one", **options(tmp_path))
    monkeypatch.setattr(
        "pr_review_harness.checks.compile", lambda *a, **k: pytest.fail("compiled"), raising=False
    )
    model = SyntaxReviewModel()
    result = review(repository, model, run_id="two", **options(tmp_path))
    assert result["incremental"]["mode"] == "unchanged"
    assert result["incremental"]["check_cache"]["hits"] == 4
    assert model.calls > 0


@pytest.mark.parametrize(
    "field",
    [
        "model",
        "budget",
        "strategy",
        "run_tests",
        "execution",
        "mode",
        "skills_sha256",
        "implementation_sha256",
        "runner",
    ],
)
def test_configuration_changes_force_full_scheduling(repository, tmp_path, field):
    values = identity(repository, DemoChatModel(), BudgetPolicy(), "ast", False, "test")
    config = configuration_digest(values, {"text": "rule", "captured_at": "one"})
    store = IncrementalStore(tmp_path / "reuse")
    plan = store.plan(repository, "pr", config)
    store.complete(repository, plan, "one")
    values[field] = {"changed": True}
    result = store.plan(repository, "pr", configuration_digest(values, {"text": "rule"}))
    assert result["mode"] == "full"
    assert result["reason"] == "configuration_changed"


def test_recalled_time_is_not_a_config_change_but_revoked_memory_is(repository, tmp_path):
    memory = MemoryStore(tmp_path / "memory.sqlite3")
    record = memory.add(repository.repo_id, "Confirmed rule", "maintainer")
    first = memory.recall_snapshot(repository.repo_id, ["pricing.py"])
    second = replace(first, manifest={**first.manifest, "captured_at": "later"})
    values = identity(repository, DemoChatModel(), BudgetPolicy(), "ast", False, "test")
    a = configuration_digest(values, {"text": first.text, **first.manifest})
    assert a == configuration_digest(values, {"text": second.text, **second.manifest})
    memory.revoke(repository.repo_id, record, reason="withdrawn")
    withdrawn = memory.recall_snapshot(repository.repo_id, ["pricing.py"])
    assert a != configuration_digest(values, {"text": withdrawn.text, **withdrawn.manifest})


def test_force_push_and_merge_base_changes_force_full_scheduling(repository, tmp_path):
    store = IncrementalStore(tmp_path / "reuse")
    store.complete(repository, store.plan(repository, "pr", "config"), "one")
    _git(repository.repo, "checkout", "-q", "-B", "replacement", repository.base_sha)
    replaced = update(repository, "z_module.py", "replacement = True\n")
    assert store.plan(replaced, "pr", "config")["reason"] == "previous_head_not_ancestor"
    advanced = Snapshot.load(repository.repo, repository.head_sha, repository.head_sha)
    assert store.plan(advanced, "pr", "config")["reason"] == "merge_base_changed"
    with store.connect() as conn:
        baseline = store.baseline(repository.repo_id, "pr")
        baseline["head_sha"] = "f" * 40
        conn.execute("UPDATE baselines SET value=?", (json.dumps(baseline),))
    assert store.plan(repository, "pr", "config")["reason"] == "previous_snapshot_unavailable"


def test_concurrent_completion_cannot_replace_newer_baseline(repository, tmp_path):
    store = IncrementalStore(tmp_path / "reuse")
    old_plan = store.plan(repository, "pr", "config")
    changed = update(repository, "z_module.py", "modified = True\n")
    newer_plan = store.plan(changed, "pr", "config")
    assert store.complete(changed, newer_plan, "new") == "recorded"
    assert store.complete(repository, old_plan, "old") == "skipped_concurrent_update"
    assert store.baseline(repository.repo_id, "pr")["head_sha"] == changed.head_sha


def test_unittest_is_never_reused_even_for_identical_snapshot(repository, tmp_path, monkeypatch):
    fixed = update(repository, "pricing.py", "def value():\n    return 1\n")
    store = IncrementalStore(tmp_path / "reuse")
    attempts = []
    original = CheckRunner._unittest

    def counted(self, *args):
        attempts.append(args)
        return original(self, *args)

    monkeypatch.setattr(CheckRunner, "_unittest", counted)
    for run_id in ("one", "two"):
        evidence = CheckRunner(fixed, run_tests=True, reuse=store, run_id=run_id).run(
            "unittest", "test_value.py"
        )
        assert evidence.head.status == "passed"
        assert evidence.head.cache is None
    assert len(attempts) == 4
    changed = update(fixed, "pricing.py", "def value():\n    return 2\n")
    evidence = CheckRunner(changed, run_tests=True, reuse=store).run("unittest", "test_value.py")
    assert evidence.same_check
    assert evidence.base.status == "passed"
    assert evidence.head.status == "failed"
    assert len(attempts) == 6


def test_syntax_cache_isolates_paths_repositories_and_compiler(repository, tmp_path):
    store = IncrementalStore(tmp_path / "reuse")
    CheckRunner(repository, reuse=store).run("syntax", "z_module.py")
    foreign = Snapshot.load(
        repository.repo, repository.base_sha, repository.head_sha, repository_identity="foreign"
    )
    foreign_check = CheckRunner(foreign, reuse=store).run("syntax", "z_module.py")
    assert foreign_check.base.cache["status"] == "miss"
    assert foreign_check.head.cache["status"] == "hit"
    # The foreign head can hit its own base compilation, but not the original repository's key.
    original = CheckRunner(repository, reuse=store).run("syntax", "z_module.py")
    other = CheckRunner(foreign, reuse=store).run("syntax", "z_module.py")
    assert original.head.cache["key"] != other.head.cache["key"]
    runner = CheckRunner(repository, reuse=store)
    runner.syntax_identity["python"] = "another compiler"
    assert runner.run("syntax", "pricing.py").head.cache["status"] == "miss"


def test_unavailable_reads_do_not_enter_cache(repository, tmp_path, monkeypatch):
    store = IncrementalStore(tmp_path / "reuse")
    monkeypatch.setattr(
        Snapshot, "read_file", lambda *a: (_ for _ in ()).throw(ValueError("unreadable"))
    )
    check = CheckRunner(repository, reuse=store).run("syntax", "pricing.py")
    assert check.head.status == "unavailable"
    with store.connect() as conn:
        assert conn.execute("SELECT count(*) FROM syntax_checks").fetchone()[0] == 0


def test_priority_does_not_hide_paths_after_context_count_limit(repository, tmp_path):
    for index in range(41):
        (repository.repo / f"module_{index:02}.py").write_text("value = 1\n")
    head = commit(repository.repo, "many files")
    snapshot = Snapshot.load(repository.repo, repository.base_sha, head)
    context = build_context(
        snapshot,
        max_chars=50000,
        priority_paths=("z_module.py", "module_40.py"),
        update_from=repository.head_sha,
    )
    assert context.items[0].path == "module_40.py"
    assert any("module_39.py: changed-file context count limit" == s for s in context.omitted)
    assert len(snapshot.changed_files) == 42


def test_numeric_github_key_and_repository_baseline_isolation(repository, tmp_path):
    assert (
        review_key(repository, {"kind": "github", "repository_id": 42, "number": 5}, "ignored")
        == "github:42:pull:5"
    )
    with pytest.raises(ValueError, match="review_key"):
        review_key(repository, None, "bad\nkey")
    store = IncrementalStore(tmp_path / "reuse")
    store.complete(repository, store.plan(repository, "pr-1", "config"), "first")
    assert store.plan(repository, "pr-2", "config")["previous"] is None
    foreign = Snapshot.load(
        repository.repo, repository.base_sha, repository.head_sha, repository_identity="foreign"
    )
    assert store.plan(foreign, "pr-1", "config")["previous"] is None


def test_incremental_plan_is_bound_to_run_artifacts(repository, tmp_path):
    settings = options(tmp_path)
    review(repository, SyntaxReviewModel(), run_id="bound", **settings)
    store = RunStore(settings["runs_dir"], "bound")
    artifact_path = store.path / "artifacts.json"
    artifacts = json.loads(artifact_path.read_text())
    artifacts["incremental"]["updated_paths"] = ["untrusted.py"]
    artifact_path.write_text(json.dumps(artifacts))
    with pytest.raises(ValueError, match="artifacts.*changed"):
        review(repository, SyntaxReviewModel(), run_id="bound", resume=True, **settings)


def test_cli_wires_incremental_options_and_restores_saved_key(repository, tmp_path, monkeypatch):
    from pr_review_harness import cli

    monkeypatch.chdir(tmp_path)
    models = []

    def model_factory(*args):
        model = SyntaxReviewModel()
        models.append(model)
        return model

    monkeypatch.setattr(cli, "_live_model", model_factory)

    def run(arguments):
        return cli._dispatch(cli.parser().parse_args(arguments))

    assert (
        run(
            [
                "review",
                "--repo",
                str(repository.repo),
                "--base",
                repository.base_sha,
                "--head",
                repository.head_sha,
                "--incremental",
                "--review-key",
                "cli-pr",
                "--run-id",
                "cli-first",
                "--out",
                str(tmp_path / "first"),
            ]
        )
        == 0
    )
    changed = update(repository, "z_module.py", "def unchanged():\n    return 2\n")
    assert (
        run(
            [
                "review",
                "--repo",
                str(changed.repo),
                "--base",
                changed.base_sha,
                "--head",
                changed.head_sha,
                "--incremental",
                "--review-key",
                "cli-pr",
                "--run-id",
                "cli-second",
                "--out",
                str(tmp_path / "second"),
            ]
        )
        == 0
    )
    report = json.loads((tmp_path / "second/review.json").read_text())
    assert report["incremental"]["mode"] == "incremental"
    assert run(["resume", "--run-id", "cli-second", "--out", str(tmp_path / "resumed")]) == 0
    assert models[-1].calls == 0
    resumed = json.loads((tmp_path / "resumed/review.json").read_text())
    assert resumed["incremental"]["review_key"] == "cli-pr"
    assert resumed["incremental"]["check_cache"] == report["incremental"]["check_cache"]
