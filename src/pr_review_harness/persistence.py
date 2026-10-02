"""Local run identity, cross-process exclusion and durable execution receipts."""

import hashlib
import json
import os
import platform
import re
import sqlite3
from contextlib import contextmanager
from dataclasses import asdict
from pathlib import Path

from langchain_core.callbacks import BaseCallbackHandler

from pr_review_harness.budget import BudgetExceeded
from pr_review_harness.execution import ExecutionPolicy
from pr_review_harness.skills import skill_files


def digest(value):
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()
    ).hexdigest()


def response_diagnostics(message, generation_info=None):
    """Keep output failure facts without duplicating an unbounded model answer."""
    metadata = {**(generation_info or {}), **getattr(message, "response_metadata", {})}
    invalid = getattr(message, "invalid_tool_calls", [])
    return {
        "message_id": getattr(message, "id", None),
        "finish_reason": metadata.get("finish_reason"),
        "tool_names": [t["name"] for t in getattr(message, "tool_calls", [])],
        "invalid_tool_calls": [
            {
                "name": t.get("name"),
                "error": str(t.get("error") or "invalid JSON")[:500],
                "arguments_sha256": digest(t.get("args")),
            }
            for t in invalid
        ],
        "content_chars": len(str(message.content)),
        "content_sha256": digest(message.content),
    }


def atomic_json(path, value):
    """Publish only complete JSON files, including after interruption."""
    import tempfile

    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".pending-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(tmp, path)
    finally:
        Path(tmp).unlink(missing_ok=True)


def model_identity(model):
    value = {"class": f"{type(model).__module__}.{type(model).__name__}", "type": model._llm_type}
    for key in (
        "model_name",
        "model",
        "openai_api_base",
        "temperature",
        "max_tokens",
        "max_retries",
        "tiktoken_model_name",
        "seed",
    ):
        item = getattr(model, key, None)
        if isinstance(item, (str, int, float, bool)):
            value[key] = item
    for key in ("extra_body", "model_kwargs"):
        item = getattr(model, key, None)
        if isinstance(item, dict):
            value[key] = item
    return value


def identity(snapshot, model, policy, strategy, run_tests, mode, execution=None):
    source = Path(__file__).parent
    return {
        "schema_version": 1,
        "repo_id": snapshot.repo_id,
        "repository_identity": snapshot.repository_identity,
        "repo": str(snapshot.repo),
        "base_sha": snapshot.base_sha,
        "head_sha": snapshot.head_sha,
        "merge_base_sha": snapshot.merge_base_sha,
        "model": model_identity(model),
        "budget": asdict(policy),
        "strategy": strategy,
        "run_tests": run_tests,
        "execution": (execution or ExecutionPolicy()).manifest(run_tests),
        "mode": mode,
        "skills_sha256": digest({path: value["content"] for path, value in skill_files().items()}),
        "implementation_sha256": digest(
            {p.name: p.read_text() for p in sorted(source.glob("*.py"))}
        ),
        "runner": {"python": platform.python_version(), "platform": platform.platform()},
    }


class RunStore:
    def __init__(self, root: Path, run_id: str):
        if not re.fullmatch(r"[a-zA-Z0-9_-]{1,80}", run_id):
            raise ValueError("run_id must contain 1–80 letters, digits, underscores or hyphens")
        self.run_id = run_id
        self.path = Path(root).resolve() / run_id
        self.path.mkdir(parents=True, exist_ok=True)
        self.db = self.path / "executions.sqlite3"
        with self.connect() as conn:
            conn.execute(
                "CREATE TABLE IF NOT EXISTS receipts (id TEXT PRIMARY KEY, "
                "kind TEXT NOT NULL, status TEXT NOT NULL, payload TEXT)"
            )

    def connect(self):
        from contextlib import closing

        @contextmanager
        def connection():
            with closing(sqlite3.connect(self.db, timeout=10)) as conn, conn:
                conn.row_factory = sqlite3.Row
                yield conn

        return connection()

    @contextmanager
    def locked(self):
        import fcntl

        with (self.path / "run.lock").open("a") as stream:
            try:
                fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise ValueError("This run is already active in another process") from exc
            try:
                yield
            finally:
                fcntl.flock(stream, fcntl.LOCK_UN)

    def create(self, manifest, artifacts):
        if (self.path / "manifest.json").exists():
            raise ValueError("run_id already exists; use resume or a new run_id")
        atomic_json(self.path / "artifacts.json", artifacts)
        value = {**manifest, "run_id": self.run_id, "artifacts_sha256": digest(artifacts)}
        atomic_json(self.path / "manifest.json", value)
        return value

    def load(self):
        manifest = json.loads((self.path / "manifest.json").read_text())
        artifacts = json.loads((self.path / "artifacts.json").read_text())
        if digest(artifacts) != manifest["artifacts_sha256"]:
            raise ValueError("Run artifacts are missing or have changed")
        return manifest, artifacts

    def validate(self, expected):
        manifest, artifacts = self.load()
        if {k: manifest.get(k) for k in expected} != expected:
            changed = [k for k, value in expected.items() if manifest.get(k) != value]
            raise ValueError("Cannot resume: changed identity fields: " + ", ".join(changed))
        return manifest, artifacts

    def receipt(self, key):
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM receipts WHERE id=?", (key,)).fetchone()
        if row is None:
            return None
        return {**dict(row), "payload": json.loads(row["payload"]) if row["payload"] else None}

    def start(self, key, kind, limit=None, *, payload=None, stage_limit=None):
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            if limit is not None:
                count = conn.execute(
                    "SELECT COUNT(*) FROM receipts WHERE kind=?", (kind,)
                ).fetchone()[0]
                if count >= limit:
                    raise BudgetExceeded(f"Global {kind} call budget exhausted ({limit})")
            if stage_limit is not None:
                count = conn.execute(
                    "SELECT COUNT(*) FROM receipts WHERE kind=? AND "
                    "json_extract(payload, '$.stage')=?",
                    (kind, payload["stage"]),
                ).fetchone()[0]
                if count >= stage_limit:
                    raise BudgetExceeded(f"{payload['stage']} {kind} attempt budget exhausted")
            conn.execute(
                "INSERT INTO receipts VALUES (?, ?, 'started', ?)",
                (key, kind, json.dumps(payload) if payload is not None else None),
            )

    def finish(self, key, payload):
        with self.connect() as conn:
            conn.execute(
                "UPDATE receipts SET status='completed', payload=? WHERE id=?",
                (json.dumps(payload, ensure_ascii=False), key),
            )

    def reset_unknown_checks(self):
        """Explicit caller authorization allows rerunning incomplete business tools."""
        with self.connect() as conn:
            conn.execute(
                "UPDATE receipts SET status='retry-authorized' WHERE kind='tool' "
                "AND status='started'"
            )

    def budget_usage(self):
        with self.connect() as conn:
            rows = conn.execute("SELECT * FROM receipts").fetchall()
        models = [r for r in rows if r["kind"] == "model"]
        usage = {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0}
        unknown = 0
        for row in models:
            payload = json.loads(row["payload"]) if row["payload"] else {}
            tokens = payload.get("usage")
            if tokens is None:
                unknown += 1
            else:
                for key in usage:
                    usage[key] += tokens.get(key, 0)
        return {
            "model_attempts": len(models),
            "tool_attempts": sum(r["kind"] == "tool" for r in rows),
            "unknown_usage_calls": unknown,
            "known_usage": usage,
            "scope": "review and summary attempts, plus verifier when attached; "
            "provider internal retries may be unreported",
        }

    def stage_attempts(self, kind, stage):
        with self.connect() as conn:
            return conn.execute(
                "SELECT COUNT(*) FROM receipts WHERE kind=? AND json_extract(payload, '$.stage')=?",
                (kind, stage),
            ).fetchone()[0]


class ModelAccounting(BaseCallbackHandler):
    """Reserve attempts before dispatch, including SDK summary calls and failed calls."""

    raise_error = True

    def __init__(
        self, policy, store=None, model=None, *, stage="review", stage_limit=None, prior=None
    ):
        self.stage = stage
        self.stage_limit = stage_limit or policy.model_calls
        self.stage_calls = 0
        self.prior = prior or {}
        self.model = model
        self.policy = policy
        self.store = store
        self.calls = self.prior.get("model_attempts", 0)
        self.known = {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0}
        self.known.update(self.prior.get("known_usage", {}))
        self.unknown = {f"prior-{i}" for i in range(self.prior.get("unknown_usage_calls", 0))}

    def on_chat_model_start(self, serialized, messages, *, run_id, **kwargs):
        from uuid import uuid4

        if self.model is not None:
            from pr_review_harness.budget import RequestCounter

            counter = RequestCounter(self.model, self.policy)
            tools = kwargs.get("invocation_params", {}).get("tools", [])
            for batch in messages:
                if counter(batch, tools=tools) > self.policy.input_limit:
                    raise BudgetExceeded("Model or summary request exceeds complete input budget")
        key = "model-" + str(run_id or uuid4())
        metadata = {
            "stage": self.stage,
            "usage": None,
            "message_sha256": digest(
                [[m.model_dump(mode="json") for m in batch] for batch in messages]
            ),
        }
        if self.store:
            self.store.start(
                key,
                "model",
                self.policy.total_model_calls,
                payload=metadata,
                stage_limit=self.stage_limit,
            )
        elif self.calls >= self.policy.total_model_calls:
            raise BudgetExceeded("Global model call budget exhausted")
        if not self.store and self.stage_calls >= self.stage_limit:
            raise BudgetExceeded(f"{self.stage} model attempt budget exhausted")
        self.stage_calls += 1
        self.calls += 1
        self.unknown.add(str(run_id))

    def on_llm_end(self, response, *, run_id, **kwargs):
        generation = response.generations[0][0]
        usage = getattr(generation.message, "usage_metadata", None)
        if usage:
            self.unknown.discard(str(run_id))
            for key in self.known:
                self.known[key] += usage.get(key, 0)
        if self.store:
            key = "model-" + str(run_id)
            previous = self.store.receipt(key)
            self.store.finish(
                key,
                {
                    **(previous["payload"] or {}),
                    "usage": usage,
                    "response": response_diagnostics(
                        generation.message, generation.generation_info
                    ),
                },
            )

    def remaining(self):
        used = self.store.stage_attempts("model", self.stage) if self.store else self.stage_calls
        return min(self.stage_limit - used, self.policy.total_model_calls - self.calls)

    def manifest(self):
        if self.store:
            return self.store.budget_usage()
        return {
            "model_attempts": self.calls,
            "known_usage": self.known,
            "unknown_usage_calls": len(self.unknown),
            "scope": "review and summary attempts; provider retries may be unreported",
        }
