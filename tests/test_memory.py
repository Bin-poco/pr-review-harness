from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from pr_review_harness.memory import MAX_SOURCE_CHARS, MAX_TEXT_CHARS, MemoryStore


def test_feedback_survives_a_new_store_and_creates_parent_directory(tmp_path: Path) -> None:
    db_path = tmp_path / "nested" / "memory.sqlite3"
    original = MemoryStore(db_path)
    record_id = original.add("owner/repo", "Validate empty input", "PR #1 review #2")

    reopened = MemoryStore(db_path)
    records = reopened.list_records("owner/repo")
    assert records[0]["id"] == record_id
    assert records[0]["source"] == "PR #1 review #2"
    assert "Validate empty input" in reopened.recall("owner/repo", ["src/api.py"])


def test_recall_isolates_repository_and_matches_directory_globs(tmp_path: Path) -> None:
    store = MemoryStore(tmp_path / "memory.db")
    store.add("a", "Global rule", "PR 1")
    store.add("a", "Direct child only", "PR 2", path_glob="src/*.py")
    store.add("a", "Recursive Python rule", "PR 3", path_glob="src/**/*.py")
    store.add("a", "Documentation rule", "PR 4", path_glob="docs/**")
    store.add("b", "Other repository", "PR 5")

    nested = store.recall("a", ["src/nested/api.py"])
    assert "Global rule" in nested
    assert "Recursive Python rule" in nested
    assert "Direct child only" not in nested
    assert "Documentation rule" not in nested
    assert "Other repository" not in nested
    direct = store.recall("a", ["src/api.py"])
    assert "Direct child only" in direct
    assert "Recursive Python rule" in direct


def test_no_paths_recalls_only_repository_wide_feedback(tmp_path: Path) -> None:
    store = MemoryStore(tmp_path / "memory.db")
    store.add("a", "Global guidance", "PR 1")
    store.add("a", "Scoped guidance", "PR 2", path_glob="src/**")
    recalled = store.recall("a", [])
    assert "Global guidance" in recalled
    assert "Scoped guidance" not in recalled


def test_expired_feedback_is_inspectable_but_not_recalled(tmp_path: Path) -> None:
    store = MemoryStore(tmp_path / "memory.db")
    record_id = store.add("a", "Expired guidance", "PR 1")
    with sqlite3.connect(store.db_path) as connection:
        connection.execute(
            "UPDATE review_memory SET expires_at = ? WHERE id = ?",
            ("2000-01-01T00:00:00+00:00", record_id),
        )
    assert store.list_records("a")[0]["text"] == "Expired guidance"
    assert store.recall("a", ["src/api.py"]) == ""


def test_dismissed_feedback_keeps_its_meaning_and_source(tmp_path: Path) -> None:
    store = MemoryStore(tmp_path / "memory.db")
    store.add("a", "Earlier null check suggestion", "PR 7 comment 3", disposition="dismissed")
    recalled = store.recall("a", ["src/api.py"])
    assert "do not authorize behavior or establish a business rule" in recalled
    entry = json.loads(recalled.splitlines()[1])
    assert entry["disposition"] == "dismissed"
    assert entry["source"] == "PR 7 comment 3"


@pytest.mark.parametrize("max_chars", [0, 1, 50, 250, 500, 1_000, 4_000])
def test_character_budget_is_strict_with_unicode_and_escaped_text(
    tmp_path: Path, max_chars: int
) -> None:
    store = MemoryStore(tmp_path / "memory.db")
    store.add("a", '审查 "约束"\n' * 700, "人工反馈")
    recalled = store.recall("a", ["src/api.py"], max_chars=max_chars)
    assert len(recalled) <= max_chars
    for line in recalled.splitlines()[1:]:
        json.loads(line)


def test_truncation_keeps_complete_record_and_marks_shortened_text(tmp_path: Path) -> None:
    store = MemoryStore(tmp_path / "memory.db")
    store.add("a", "long feedback " * 500, "PR 1", disposition="dismissed")
    recalled = store.recall("a", ["src/api.py"], max_chars=600)
    entry = json.loads(recalled.splitlines()[1])
    assert entry["disposition"] == "dismissed"
    assert entry["text"].endswith(" [truncated]")


def test_sql_special_characters_are_stored_as_data(tmp_path: Path) -> None:
    store = MemoryStore(tmp_path / "memory.db")
    repo_id = "repo'; DROP TABLE review_memory; --"
    text = "Prefer parameterized SQL: 'quoted'; --"
    store.add(repo_id, text, "PR '1'")
    store.add("other", "Still writable", "PR 2")
    assert store.list_records(repo_id)[0]["text"] == text
    assert len(store.list_records("other")) == 1
    assert store.recall("repo", ["api.py"]) == ""


@pytest.mark.parametrize(
    "arguments",
    [
        {"text": " "},
        {"text": "x" * (MAX_TEXT_CHARS + 1)},
        {"source": ""},
        {"source": "x" * (MAX_SOURCE_CHARS + 1)},
        {"repo_id": ""},
        {"path_glob": ""},
        {"disposition": "unconfirmed"},
        {"ttl_days": 0},
        {"ttl_days": -1},
        {"ttl_days": True},
    ],
)
def test_invalid_feedback_does_not_write(tmp_path: Path, arguments: dict[str, object]) -> None:
    store = MemoryStore(tmp_path / "memory.db")
    values = {"repo_id": "a", "text": "rule", "source": "PR 1", **arguments}
    with pytest.raises(ValueError):
        store.add(**values)
    assert store.list_records("a") == []


@pytest.mark.parametrize("max_chars", [-1, True, 1.5])
def test_invalid_budget_is_rejected(tmp_path: Path, max_chars: object) -> None:
    store = MemoryStore(tmp_path / "memory.db")
    with pytest.raises(ValueError):
        store.recall("a", [], max_chars=max_chars)
