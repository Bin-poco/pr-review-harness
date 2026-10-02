"""Human feedback links, topic groups and additive migration remain explicit."""

import json

import pytest
from test_runtime import make_snapshot

from pr_review_harness.memory import MemoryStore
from pr_review_harness.runtime import DemoChatModel, review


def test_feedback_links_and_default_scope(tmp_path):
    snapshot = make_snapshot(tmp_path)
    report = review(snapshot, DemoChatModel(), run_tests=True)
    store = MemoryStore(tmp_path / "memory.db")
    finding = report["findings"][0]
    record = store.add_feedback(
        snapshot.repo_id,
        report,
        "Confirmed rule",
        "human review",
        finding_id=finding["id"],
        rule_key="pricing.discount",
        disposition="accepted",
    )
    row = store.list_records(snapshot.repo_id)[0]
    assert row["id"] == record
    assert row["source_run_id"] == report["run_id"]
    assert row["finding_id"] == finding["id"]
    assert row["path_glob"] == "pricing.py"
    recalled = store.recall_snapshot(snapshot.repo_id, ["pricing.py"])
    assert recalled.manifest["records"][0]["source_run_id"] == report["run_id"]
    assert json.loads(recalled.text.splitlines()[1])["rule_key"] == "pricing.discount"
    with pytest.raises(ValueError, match="finding_id"):
        store.add_feedback(snapshot.repo_id, report, "Bad link", "human", finding_id="invented")
    with pytest.raises(ValueError, match="different repository"):
        store.add_feedback("another", report, "Bad link", "human")


def test_topic_group_scopes_and_revision_preserve_links(tmp_path):
    store = MemoryStore(tmp_path / "memory.db")
    old = store.add(
        "repo",
        "Prior guidance",
        "old",
        rule_key="discount",
        source_run_id="run1",
        finding_id="finding1",
        path_glob="src/**",
    )
    newer = store.add("repo", "Different guidance", "new", rule_key="discount", path_glob="src/**")
    store.add("repo", "Unrelated path", "other", rule_key="discount", path_glob="docs/**")
    recalled = store.recall_snapshot("repo", ["src/pricing.py"])
    group = recalled.manifest["conflict_groups"][0]
    assert set(group["record_ids"]) == {old, newer}
    assert group["eligible_count"] == 2
    assert json.loads(recalled.text.splitlines()[1])["topic_group"]["eligible_count"] == 2
    replacement = store.revise("repo", old, "Clarified", "updated", reason="maintainer")
    row = next(r for r in store.list_records("repo") if r["id"] == replacement)
    assert row["rule_key"] == "discount" and row["finding_id"] == "finding1"
    store.revoke("repo", newer, reason="explicit conflict resolution")
    assert store.recall_snapshot("repo", ["src/pricing.py"]).manifest["conflict_groups"] == []


def test_invalid_link_is_atomic(tmp_path):
    store = MemoryStore(tmp_path / "memory.db")
    with pytest.raises(ValueError, match="requires source_run_id"):
        store.add("repo", "rule", "human", finding_id="orphan")
    assert store.list_records("repo") == []
