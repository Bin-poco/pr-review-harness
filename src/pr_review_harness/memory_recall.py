"""Bounded, path-based recall from one immutable repository feedback pool.

The pool is persisted as run input, never exposed as a bulk model memory file.
Only a bounded selection reaches ContextManager's existing request guard.
"""

import hashlib
import json

from pr_review_harness.memory import _RECALL_HEADER, MemoryRecall, _bounded_record, _matches_path

MAX_CANDIDATE_RECORDS = 1_000
MAX_CANDIDATE_BYTES = 1_000_000


def _encode(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()


def _pool_hash(pool):
    return hashlib.sha256(_encode({k: v for k, v in pool.items() if k != "sha256"})).hexdigest()


def freeze_feedback(connection, repo_id, paths, max_chars, now):
    """Stream a consistent DB view; retain at most three bounded priority buckets."""
    buckets = [[], [], []]
    sizes = [2, 2, 2]  # UTF-8 JSON list brackets.
    eligible = 0
    rows = connection.execute(
        "SELECT * FROM review_memory WHERE repo_id=? AND expires_at>? AND status='active' "
        "ORDER BY created_at DESC, id DESC",
        (repo_id, now),
    )
    for row in rows:
        eligible += 1
        record = dict(row)
        scope = record["path_glob"]
        tier = 1 if scope == "*" else 0 if any(_matches_path(scope, p) for p in paths) else 2
        size = len(_encode(record)) + int(bool(buckets[tier]))
        if len(buckets[tier]) < MAX_CANDIDATE_RECORDS and sizes[tier] + size <= MAX_CANDIDATE_BYTES:
            buckets[tier].append(record)
            sizes[tier] += size
    records, used = [], 2
    for bucket in buckets:
        for record in bucket:
            size = len(_encode(record)) + int(bool(records))
            if len(records) < MAX_CANDIDATE_RECORDS and used + size <= MAX_CANDIDATE_BYTES:
                records.append(record)
                used += size
    pool = {
        "version": 1,
        "repo_id": repo_id,
        "records": records,
        "eligible_count": eligible,
        "omitted_count": eligible - len(records),
        "record_bytes": used,
        "max_records": MAX_CANDIDATE_RECORDS,
        "max_record_bytes": MAX_CANDIDATE_BYTES,
        "ranking": "changed-path scoped, global, other scoped; newest within each group",
    }
    pool["sha256"] = _pool_hash(pool)
    synced = connection.execute("SELECT * FROM memory_sync WHERE repo_id=?", (repo_id,)).fetchone()
    recalled = select_frozen(
        pool, [{"path": p, "reason": "changed-path match"} for p in paths], max_chars
    )
    return MemoryRecall(
        recalled.text,
        {
            **recalled.manifest,
            "captured_at": now,
            "candidate_pool": pool,
            "storage": (
                {
                    "bundle_sha256": synced["bundle_sha256"],
                    "origin": json.loads(synced["origin"]),
                    "local_changes": bool(synced["local_changes"]),
                }
                if synced
                else {"kind": "local-sqlite"}
            ),
        },
    )


def validate_pool(pool, repo_id):
    """Reject incompatible, cross-repository or corrupted frozen run inputs."""
    if not isinstance(pool, dict) or pool.get("version") != 1 or pool.get("repo_id") != repo_id:
        raise ValueError("Frozen feedback pool version or repository does not match")
    records = pool.get("records")
    if (
        not isinstance(records, list)
        or len(records) > MAX_CANDIDATE_RECORDS
        or len(_encode(records)) > MAX_CANDIDATE_BYTES
        or pool.get("record_bytes") != len(_encode(records))
        or type(pool.get("eligible_count")) is not int
        or type(pool.get("omitted_count")) is not int
        or pool["omitted_count"] < 0
        or pool["eligible_count"] != len(records) + pool["omitted_count"]
        or pool.get("sha256") != _pool_hash(pool)
    ):
        raise ValueError("Frozen feedback pool exceeds limits or has an invalid digest")
    ids, uids = set(), set()
    for record in records:
        if (
            record.get("repo_id") != repo_id
            or record.get("status") != "active"
            or record.get("disposition") not in {"accepted", "dismissed"}
            or type(record.get("id")) is not int
            or record["id"] < 1
            or not all(
                isinstance(record.get(k), str)
                for k in ("record_uid", "path_glob", "text", "source")
            )
            or record["id"] in ids
            or record["record_uid"] in uids
        ):
            raise ValueError("Frozen feedback pool contains invalid or duplicate records")
        ids.add(record["id"])
        uids.add(record["record_uid"])


def select_frozen(pool, paths, max_chars):
    """Select by actual file activity, without rereading live feedback storage.

    ``paths`` is newest activity first, followed by changed paths.
    Each rule appears once. Global rules follow scoped rules. Omitted candidate
    records are reported separately because their scopes are no longer available.
    """
    if type(max_chars) is not int or max_chars < 0:
        raise ValueError("max_chars must be a non-negative integer")
    unique = {}
    for item in paths:
        unique.setdefault(item["path"], item)
    paths = list(unique.values())
    eligible = []
    for index, record in enumerate(pool["records"]):
        scope = record["path_glob"]
        matches = [p for p in paths if _matches_path(scope, p["path"])] if scope != "*" else []
        if scope == "*" or matches:
            priority = next((i for i, p in enumerate(paths) if p in matches), len(paths))
            eligible.append((priority, index, record, matches))
    eligible.sort(key=lambda item: item[:2])
    groups = {}
    for _, _, record, _ in eligible:
        key = record.get("rule_key")
        if key is not None:
            group = groups.setdefault(
                key,
                {
                    "rule_key": key,
                    "record_ids": [],
                    "eligible_count": 0,
                    "status": "unresolved topic; not a proven contradiction",
                },
            )
            group["eligible_count"] += 1
            if len(group["record_ids"]) < 32:
                group["record_ids"].append(record["id"])
    output = _RECALL_HEADER[:max_chars] if eligible else ""
    entries = []
    for _, _, original, matches in eligible:
        record = dict(original)
        group = groups.get(record.get("rule_key"))
        if group and group["eligible_count"] > 1:
            record["topic_group"] = group
        encoded = _bounded_record(record, max_chars - len(output) - 1)
        if encoded is None:
            continue
        output += encoded + "\n"
        entries.append(
            {
                "id": record["id"],
                "uid": record["record_uid"],
                "scope": record["path_glob"],
                "source": record["source"],
                "disposition": record["disposition"],
                "source_run_id": record.get("source_run_id"),
                "finding_id": record.get("finding_id"),
                "rule_key": record.get("rule_key"),
                "reason": matches[0]["reason"] if matches else "repository-wide",
                "matched_paths": [p["path"] for p in matches],
                "truncated": json.loads(encoded)["text"] != record["text"],
            }
        )
    return MemoryRecall(
        output,
        {
            "repo_id": pool["repo_id"],
            "pool_sha256": pool["sha256"],
            "max_chars": max_chars,
            "used_chars": len(output),
            "sha256": hashlib.sha256(output.encode()).hexdigest(),
            "records": entries,
            "eligible_count": len(eligible),
            "omitted_count": len(eligible) - len(entries),
            "candidate_pool_omitted_count": pool["omitted_count"],
            "ranking": "recent file activity, changed paths, global; frozen order",
            "conflict_groups": [
                g
                for k, g in groups.items()
                if g["eligible_count"] > 1 and any(e["rule_key"] == k for e in entries)
            ],
            "conflict_detection": "explicit rule_key only; keyless records unchecked",
        },
    )
