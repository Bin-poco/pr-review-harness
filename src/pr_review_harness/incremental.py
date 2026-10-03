"""Update-aware scheduling and a local cache for pure Python compilation only."""

import json
import sqlite3
from contextlib import closing, contextmanager
from pathlib import Path

from pr_review_harness.persistence import digest
from pr_review_harness.snapshot import Snapshot


def configuration_digest(identity: dict, memory: dict) -> str:
    """Versions belong to the plan, while every review input belongs to this digest."""
    return digest(
        {
            "configuration": {
                key: identity[key]
                for key in (
                    "model",
                    "budget",
                    "strategy",
                    "run_tests",
                    "execution",
                    "mode",
                    "skills_sha256",
                    "implementation_sha256",
                    "runner",
                )
            },
            "memory": {key: value for key, value in memory.items() if key != "captured_at"},
        }
    )


def review_key(snapshot: Snapshot, source: dict | None, explicit: str | None) -> str:
    if source and source.get("kind") == "github":
        # Use the server's numeric repository identity, including across renames.
        key = f"github:{source['repository_id']}:pull:{source['number']}"
    else:
        key = explicit or f"refs:{snapshot.base_ref}:{snapshot.head_ref}"
    if not isinstance(key, str) or not key or len(key) > 512 or any(ord(c) < 32 for c in key):
        raise ValueError("review_key must be a nonempty label of at most 512 characters")
    return key


class IncrementalStore:
    """Trusted local state. Never import caches or old findings from a PR checkout.

    Baselines record completed main stages, not reviewer approval. Updates use a
    compare-and-swap so an older concurrent review cannot overwrite a newer one.
    """

    MAX_CHECKS = 4096
    MAX_BASELINES = 1024

    def __init__(self, directory: Path):
        self.path = Path(directory).expanduser().resolve() / "incremental.sqlite3"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS baselines (
                    repo_id TEXT NOT NULL, review_key TEXT NOT NULL,
                    value TEXT NOT NULL,
                    PRIMARY KEY (repo_id, review_key)
                );
                CREATE TABLE IF NOT EXISTS syntax_checks (
                    cache_key TEXT PRIMARY KEY, value TEXT NOT NULL
                );
                """
            )

    @contextmanager
    def connect(self):
        with closing(sqlite3.connect(self.path, timeout=10)) as conn, conn:
            yield conn

    def baseline(self, repo_id, key):
        with self.connect() as conn:
            row = conn.execute(
                "SELECT value FROM baselines WHERE repo_id=? AND review_key=?", (repo_id, key)
            ).fetchone()
        return json.loads(row[0]) if row else None

    def plan(self, snapshot: Snapshot, key: str, config: str) -> dict:
        previous = self.baseline(snapshot.repo_id, key)
        plan = {
            "enabled": True,
            "review_key": key,
            "configuration_sha256": config,
            "scope": "full merge-base to current head; updated paths are scheduling hints",
            "previous": previous,
            "updated_paths": [],
            "mode": "full",
            "reason": "no_completed_baseline",
            "findings_reused": False,
        }
        if previous is None:
            return plan
        if previous["configuration_sha256"] != config:
            plan["reason"] = "configuration_changed"
        elif previous["merge_base_sha"] != snapshot.merge_base_sha:
            plan["reason"] = "merge_base_changed"
        elif previous["head_sha"] == snapshot.head_sha:
            plan.update(mode="unchanged", reason="same_snapshot_fresh_model")
        else:
            try:
                delta = Snapshot.load(snapshot.repo, previous["head_sha"], snapshot.head_sha)
                if delta.merge_base_sha != previous["head_sha"]:
                    plan["reason"] = "previous_head_not_ancestor"
                else:
                    plan.update(
                        mode="incremental",
                        reason="ancestor_with_same_merge_base_and_configuration",
                        updated_paths=[item.path for item in delta.changed_files],
                    )
            except ValueError:
                plan["reason"] = "previous_snapshot_unavailable"
        return plan

    def complete(self, snapshot: Snapshot, plan: dict, run_id: str) -> str:
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT value FROM baselines WHERE repo_id=? AND review_key=?",
                (snapshot.repo_id, plan["review_key"]),
            ).fetchone()
            current = json.loads(row[0]) if row else None
            if current and current["run_id"] == run_id:
                return "already_recorded"
            if current != plan["previous"]:
                return "skipped_concurrent_update"
            value = {
                "run_id": run_id,
                "head_sha": snapshot.head_sha,
                "merge_base_sha": snapshot.merge_base_sha,
                "configuration_sha256": plan["configuration_sha256"],
            }
            conn.execute(
                "INSERT OR REPLACE INTO baselines (repo_id, review_key, value) VALUES (?, ?, ?)",
                (snapshot.repo_id, plan["review_key"], json.dumps(value)),
            )
            conn.execute(
                "DELETE FROM baselines WHERE rowid NOT IN "
                "(SELECT rowid FROM baselines ORDER BY rowid DESC LIMIT ?)",
                (self.MAX_BASELINES,),
            )
        return "recorded"

    def get_check(self, key: str) -> dict | None:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT value FROM syntax_checks WHERE cache_key=?", (key,)
            ).fetchone()
        if not row:
            return None
        value = json.loads(row[0])
        # Only successful compilation or deterministic compilation failure is reusable.
        if (
            value.get("status") not in {"passed", "failed"}
            or value.get("exit_code") != (0 if value.get("status") == "passed" else 1)
            or not isinstance(value.get("output"), str)
            or len(value["output"]) > 32000
        ):
            return None
        return value

    def put_check(self, key: str, value: dict):
        if value["status"] not in {"passed", "failed"} or len(value["output"]) > 32000:
            return
        with self.connect() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO syntax_checks (cache_key, value) VALUES (?, ?)",
                (key, json.dumps(value)),
            )
            conn.execute(
                "DELETE FROM syntax_checks WHERE rowid NOT IN "
                "(SELECT rowid FROM syntax_checks ORDER BY rowid DESC LIMIT ?)",
                (self.MAX_CHECKS,),
            )

    def recent_checks(self, limit: int) -> list[dict]:
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT cache_key, value FROM syntax_checks ORDER BY rowid DESC LIMIT ?", (limit,)
            ).fetchall()
        return [{"key": key, "value": json.loads(value)} for key, value in rows]

    def import_checks(self, entries: list[dict]):
        """Install an already validated batch atomically; baselines are never imported."""
        with self.connect() as conn:
            conn.executemany(
                "INSERT OR REPLACE INTO syntax_checks (cache_key, value) VALUES (?, ?)",
                [(entry["key"], json.dumps(entry["value"])) for entry in entries],
            )
            conn.execute(
                "DELETE FROM syntax_checks WHERE rowid NOT IN "
                "(SELECT rowid FROM syntax_checks ORDER BY rowid DESC LIMIT ?)",
                (self.MAX_CHECKS,),
            )


def cache_manifest(evidence) -> dict:
    runs = [run for item in evidence for run in (item.base, item.head)]
    return {
        "eligible_kind": "syntax",
        "hits": sum(bool(run.cache and run.cache["status"] == "hit") for run in runs),
        "misses": sum(bool(run.cache and run.cache["status"] == "miss") for run in runs),
        "unittest_reused": False,
        "scope": "evidence in this review; resumed receipts retain original cache provenance",
    }
