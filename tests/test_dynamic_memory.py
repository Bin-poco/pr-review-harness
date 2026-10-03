"""Exercise scoped feedback across real tool reads, budgets and persisted runs."""

import json
import sqlite3
from copy import deepcopy
from dataclasses import replace

import pytest
from langchain_core.messages import AIMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from pydantic import Field

from pr_review_harness.budget import BudgetPolicy
from pr_review_harness.demo import _git
from pr_review_harness.incremental import configuration_digest
from pr_review_harness.memory import MemoryStore
from pr_review_harness.memory_recall import select_frozen, validate_pool
from pr_review_harness.persistence import identity
from pr_review_harness.report import render_markdown
from pr_review_harness.runtime import DemoChatModel, ReviewFailure, review
from pr_review_harness.snapshot import Snapshot


@pytest.fixture
def snapshot(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.name", "Harness Tests")
    _git(repo, "config", "user.email", "tests@example.invalid")
    _git(repo, "config", "commit.gpgsign", "false")
    (repo / "pricing.py").write_text("def price():\n    return 1\n")
    (repo / "consumer.py").write_text("def consume(callback):\n    return callback()\n")
    (repo / "test_consumer.py").write_text("def test_consumer():\n    pass\n")
    _git(repo, "add", ".")
    _git(repo, "-c", "core.hooksPath=/dev/null", "commit", "-qm", "base")
    base = _git(repo, "rev-parse", "HEAD")
    (repo / "pricing.py").write_text("def price():\n    return 2\n")
    _git(repo, "add", ".")
    _git(repo, "-c", "core.hooksPath=/dev/null", "commit", "-qm", "update")
    return Snapshot.load(repo, base, "HEAD")


class FileProbeModel(DemoChatModel):
    actions: list[dict] = Field(default_factory=list)
    systems: list[str] = Field(default_factory=list)
    prompts: list[str] = Field(default_factory=list)
    fail_at: int | None = None

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        completed = {m.tool_call_id for m in messages if m.type == "tool"}
        index = next(i for i in range(len(self.actions) + 1) if f"probe-{i}" not in completed)
        if index == self.fail_at:
            raise RuntimeError("interrupted after file read")
        self.systems.append("\n".join(str(m.content) for m in messages if m.type == "system"))
        self.prompts.append(str(messages))
        action = (
            self.actions[index]
            if index < len(self.actions)
            else {"name": "submit_review", "args": {"findings": []}}
        )
        return ChatResult(
            generations=[
                ChatGeneration(
                    message=AIMessage(content="", tool_calls=[{**action, "id": f"probe-{index}"}])
                )
            ]
        )


def read(path, **kwargs):
    return {"name": "read_code", "args": {"path": path, **kwargs}}


def records(text):
    return [json.loads(line) for line in text.splitlines()[1:] if line.startswith("{")]


def test_frozen_pool_isolates_lifecycle_repository_and_future_edits(tmp_path):
    store = MemoryStore(tmp_path / "memory.db")
    changed = store.add("repo", "Changed rule", "human", "src/*.py")
    later = store.add("repo", "Original test contract", "human", "tests/**/*.py")
    dismissed = store.add(
        "repo", "Rejected suggestion", "human", "tests/**/*.py", disposition="dismissed"
    )
    revoked = store.add("repo", "Revoked rule", "human")
    store.revoke("repo", revoked, reason="obsolete")
    expired = store.add("repo", "Expired rule", "human")
    store.add("other", "Foreign rule", "human", "tests/**/*.py")
    with sqlite3.connect(store.db_path) as connection:
        connection.execute(
            "UPDATE review_memory SET expires_at='2000-01-01' WHERE id=?", (expired,)
        )
    frozen = store.freeze_snapshot("repo", ["src/main.py"])
    pool = frozen.manifest["candidate_pool"]
    validate_pool(pool, "repo")
    assert {r["id"] for r in pool["records"]} == {changed, later, dismissed}
    assert [r["id"] for r in records(frozen.text)] == [changed]
    store.revise("repo", later, "New test contract", "human", reason="new contract")
    selected = select_frozen(
        pool, [{"path": "tests/nested/test_main.py", "reason": "read_code result"}], 4000
    )
    assert "Original test contract" in selected.text and "New test contract" not in selected.text
    assert any(r["disposition"] == "dismissed" for r in records(selected.text))
    assert "do not authorize" in selected.text


def test_reads_and_search_matches_activate_rules_but_lists_and_failed_reads_do_not(
    snapshot, tmp_path
):
    store = MemoryStore(tmp_path / "memory.db")
    main = store.add(snapshot.repo_id, "PRICE_CONTRACT", "human", "pricing.py")
    consumer = store.add(snapshot.repo_id, "CONSUMER_CONTRACT", "human", "consumer.py")
    test = store.add(snapshot.repo_id, "TEST_CONTRACT", "human", "test_consumer.py")
    missing = store.add(snapshot.repo_id, "MISSING_CONTRACT", "human", "missing.py")
    model = FileProbeModel(
        actions=[
            {"name": "list_code_files", "args": {"pattern": "*.py"}},
            read("missing.py"),
            read("consumer.py"),
            {"name": "search_code", "args": {"query": "test_consumer"}},
            read("consumer.py", version="base"),
        ]
    )
    frozen = store.freeze_snapshot(snapshot.repo_id, ["pricing.py"])
    report = review(snapshot, model, memory=frozen, runs_dir=tmp_path / "runs", run_id="activity")
    requests = report["context"]["assembly"]["requests"]
    assert all(r["memory_record_ids"] == [main] for r in requests[:3])
    assert requests[3]["memory_record_ids"] == [consumer, main]
    assert set(requests[4]["memory_record_ids"]) == {test, consumer, main}
    assert requests[5]["memory_record_ids"] == [consumer, test, main]
    assert all(missing not in r["memory_record_ids"] for r in requests)
    assert all(len(r["memory_record_ids"]) == len(set(r["memory_record_ids"])) for r in requests)
    assert "CONSUMER_CONTRACT" not in model.systems[0]
    assert "TEST_CONTRACT" not in model.systems[3]
    assert "TEST_CONTRACT" in model.systems[4]
    assert requests[4]["memory_recall"]["displayed_records"][0]["reason"] == "search_code match"
    assert "冻结候选规则" in render_markdown(report)


def test_resume_recalls_from_frozen_pool_after_live_store_is_removed(
    snapshot, tmp_path, monkeypatch
):
    store = MemoryStore(tmp_path / "memory.db")
    record = store.add(snapshot.repo_id, "FROZEN_CONSUMER_CONTRACT", "human", "consumer.py")
    frozen = store.freeze_snapshot(snapshot.repo_id, ["pricing.py"])
    assert frozen.text == ""
    options = {"runs_dir": tmp_path / "runs", "run_id": "resume"}
    actions = [read("consumer.py")]
    with pytest.raises(ReviewFailure):
        review(snapshot, FileProbeModel(actions=actions, fail_at=1), memory=frozen, **options)
    store.revise(snapshot.repo_id, record, "NEW_CONSUMER_CONTRACT", "human", reason="changed")
    store.db_path.unlink()
    monkeypatch.setattr(
        MemoryStore, "_connect", lambda *_: pytest.fail("Must not reopen memory DB")
    )
    model = FileProbeModel(actions=actions)
    report = review(snapshot, model, memory="Replacement caller input", resume=True, **options)
    assert len(report["trace"]) == 2
    assert "FROZEN_CONSUMER_CONTRACT" in model.systems[0]
    assert "NEW_CONSUMER_CONTRACT" not in model.systems[0]
    last = report["context"]["assembly"]["requests"][-1]
    assert last["memory_record_ids"] == [record]
    assert last["memory_recall"]["pool_sha256"] == frozen.manifest["pool_sha256"]
    completed = review(snapshot, FileProbeModel(actions=actions, fail_at=1), resume=True, **options)
    assert completed["budget_usage"] == report["budget_usage"]


@pytest.mark.parametrize("limit", ["count", "bytes"])
def test_pool_limits_prioritize_initial_paths_and_report_omissions(tmp_path, monkeypatch, limit):
    import pr_review_harness.memory_recall as recall

    store = MemoryStore(tmp_path / "memory.db")
    important = store.add("repo", "Main", "human", "main.py")
    global_id = store.add("repo", "Global", "human")
    for i in range(6):
        store.add("repo", "规则" * 100, "human", f"unseen_{i}.py")
    if limit == "count":
        monkeypatch.setattr(recall, "MAX_CANDIDATE_RECORDS", 3)
    else:
        monkeypatch.setattr(recall, "MAX_CANDIDATE_BYTES", 1600)
    frozen = store.freeze_snapshot("repo", ["main.py"])
    pool = frozen.manifest["candidate_pool"]
    validate_pool(pool, "repo")
    assert [r["id"] for r in pool["records"]][:2] == [important, global_id]
    assert pool["omitted_count"] > 0
    assert pool["eligible_count"] == len(pool["records"]) + pool["omitted_count"]
    assert pool["record_bytes"] <= recall.MAX_CANDIDATE_BYTES
    assert len(pool["records"]) <= recall.MAX_CANDIDATE_RECORDS
    assert frozen.manifest["candidate_pool_omitted_count"] == pool["omitted_count"]


def test_dynamic_rules_share_character_and_complete_token_budgets(snapshot, tmp_path):
    store = MemoryStore(tmp_path / "memory.db")
    for _ in range(8):
        store.add(snapshot.repo_id, '边界"\\\n' * 800, "human", "consumer.py", rule_key="contract")
    policy = BudgetPolicy(memory_chars=5000, window_tokens=65536, window_source="explicit")
    frozen = store.freeze_snapshot(snapshot.repo_id, ["pricing.py"], policy.memory_chars)
    model = FileProbeModel(actions=[read("consumer.py")])
    report = review(snapshot, model, memory=frozen, budget=policy)
    request = report["context"]["assembly"]["requests"][-1]
    assert request["size"] <= request["limit"]
    assert request["memory_display_chars"] <= policy.memory_chars
    assert all(r["source"] == "human" for r in records(request["memory_display_text"]))
    recall = request["memory_recall"]
    assert recall["eligible_count"] == 8
    assert recall["omitted_count"] + len(recall["records"]) == 8
    assert recall["display_omitted_count"] > 0 or recall["omitted_count"] > 0
    assert recall["conflict_groups"][0]["eligible_count"] == 8
    assert not any("candidate_pool" in system for system in model.systems)


def test_related_rule_changes_invalidate_incremental_even_if_startup_text_is_unchanged(
    snapshot, tmp_path
):
    store = MemoryStore(tmp_path / "memory.db")
    record = store.add(snapshot.repo_id, "OLD", "human", "consumer.py")
    first = store.freeze_snapshot(snapshot.repo_id, ["pricing.py"])
    second = store.freeze_snapshot(snapshot.repo_id, ["pricing.py"])
    config = identity(snapshot, FileProbeModel(), BudgetPolicy(), "ast", False, "live", None)

    def digest(memory):
        return configuration_digest(config, {"text": memory.text, **memory.manifest})

    assert digest(first) == digest(second)
    store.revise(snapshot.repo_id, record, "NEW", "human", reason="new contract")
    revised = store.freeze_snapshot(snapshot.repo_id, ["pricing.py"])
    assert first.text == revised.text == ""
    assert digest(first) != digest(revised)


def test_pool_corruption_or_foreign_repository_is_rejected_before_model(snapshot, tmp_path):
    store = MemoryStore(tmp_path / "memory.db")
    store.add(snapshot.repo_id, "Rule", "human", "consumer.py")
    frozen = store.freeze_snapshot(snapshot.repo_id, ["pricing.py"])
    corrupted = deepcopy(frozen.manifest)
    corrupted["candidate_pool"]["records"][0]["text"] = "tampered"
    with pytest.raises(ValueError, match="invalid digest"):
        review(snapshot, FileProbeModel(), memory=replace(frozen, manifest=corrupted))
    foreign = deepcopy(frozen.manifest)
    foreign["candidate_pool"]["repo_id"] = "foreign"
    with pytest.raises(ValueError, match="repository"):
        review(snapshot, FileProbeModel(), memory=replace(frozen, manifest=foreign))


def test_virtual_memory_file_does_not_expose_unselected_candidate_pool(snapshot, tmp_path):
    store = MemoryStore(tmp_path / "memory.db")
    store.add(snapshot.repo_id, "NOT_YET_RECALLED_CONSUMER_RULE", "human", "consumer.py")
    model = FileProbeModel(
        actions=[
            {"name": "read_file", "args": {"file_path": "/memories/repository.md"}},
            read("consumer.py"),
        ]
    )
    report = review(snapshot, model, memory=store.freeze_snapshot(snapshot.repo_id, ["pricing.py"]))
    assert "NOT_YET_RECALLED_CONSUMER_RULE" not in model.prompts[1]
    assert "NOT_YET_RECALLED_CONSUMER_RULE" in model.systems[2]
    assert report["context"]["assembly"]["requests"][1]["memory_record_ids"] == []


class CompactingFileProbe(FileProbeModel):
    summary_calls: int = 0

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        from pr_review_harness.working_context import LEDGER_HEADER

        system = next((m for m in messages if m.type == "system"), None)
        ledger = (
            next(
                (
                    b["text"]
                    for b in system.content_blocks
                    if b.get("text", "").startswith(LEDGER_HEADER)
                ),
                None,
            )
            if system
            else None
        )
        if ledger is None:
            self.summary_calls += 1
            return ChatResult(
                generations=[ChatGeneration(message=AIMessage(content="Earlier chat omitted."))]
            )
        self.systems.append(str(system.content))
        index = json.loads(ledger[len(LEDGER_HEADER) :])["tool_calls"]
        action = (
            self.actions[index]
            if index < len(self.actions)
            else {"name": "submit_review", "args": {"findings": []}}
        )
        return ChatResult(
            generations=[
                ChatGeneration(
                    message=AIMessage(content="", tool_calls=[{**action, "id": f"compact-{index}"}])
                )
            ]
        )


def test_recalled_rule_survives_real_native_history_compaction(snapshot, tmp_path, monkeypatch):
    from pr_review_harness.checks import CheckRunner

    original = CheckRunner._run_version

    def verbose(self, *args):
        return replace(original(self, *args), output="x" * 7000)

    monkeypatch.setattr(CheckRunner, "_run_version", verbose)
    store = MemoryStore(tmp_path / "memory.db")
    record = store.add(snapshot.repo_id, "CONSUMER_RULE_AFTER_COMPACTION", "human", "consumer.py")
    model = CompactingFileProbe(
        actions=[
            read("consumer.py"),
            {"name": "run_check", "args": {"kind": "syntax", "path": "consumer.py"}},
        ]
    )
    report = review(
        snapshot,
        model,
        memory=store.freeze_snapshot(snapshot.repo_id, ["pricing.py"]),
        budget=BudgetPolicy(request_chars=24000),
    )
    assert model.summary_calls >= 1
    assert "CONSUMER_RULE_AFTER_COMPACTION" in model.systems[-1]
    assert report["context"]["assembly"]["requests"][-1]["memory_record_ids"] == [record]
