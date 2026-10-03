"""Bounded, versioned human feedback; GitHub is a read-only source during review."""

import base64
import hashlib
import json
import re
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import quote

from pr_review_harness.memory import (
    MAX_REPO_CHARS,
    MAX_SCOPE_CHARS,
    MAX_SOURCE_CHARS,
    MAX_TEXT_CHARS,
    MAX_TTL_DAYS,
    MemoryStore,
    _feedback_links,
    _text,
)
from pr_review_harness.snapshot import _safe_path

DEFAULT_MEMORY_PATH = ".harness/feedback.json"
MAX_BUNDLE_BYTES = 1_000_000
MAX_BUNDLE_RECORDS = 1_000
_FIELDS = {
    "uid",
    "text",
    "source",
    "path_glob",
    "disposition",
    "created_at",
    "expires_at",
    "source_run_id",
    "finding_id",
    "rule_key",
    "status",
    "replaced_by",
    "status_reason",
    "updated_at",
}


def _timestamp(value, field):
    if not isinstance(value, str) or len(value) > 40:
        raise ValueError(f"{field} must be a timezone-aware timestamp")
    try:
        result = datetime.fromisoformat(value)
    except ValueError:
        raise ValueError(f"{field} must be a timezone-aware timestamp") from None
    if result.tzinfo is None or result.utcoffset() != timedelta(0):
        raise ValueError(f"{field} must use UTC")
    if result.isoformat() != value:
        raise ValueError(f"{field} must use canonical ISO UTC format")
    return result


def _encode(bundle):
    try:
        return json.dumps(
            bundle, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode()
    except (TypeError, ValueError, RecursionError):
        raise ValueError("Memory bundle must contain bounded JSON data") from None


def bundle_hash(bundle):
    return hashlib.sha256(_encode(bundle)).hexdigest()


def validate_bundle(bundle, repo_id):
    """Reject identity, schema and lifecycle errors before any database writes."""
    if len(_encode(bundle)) > MAX_BUNDLE_BYTES:
        raise ValueError("Memory bundle exceeds the supported size")
    if not isinstance(bundle, dict) or set(bundle) != {
        "schema_version",
        "kind",
        "repo_id",
        "records",
    }:
        raise ValueError("Unsupported memory bundle schema")
    if type(bundle["schema_version"]) is not int or bundle["schema_version"] != 1:
        raise ValueError("Unsupported memory bundle version")
    if bundle["kind"] != "pr-review-human-feedback" or bundle["repo_id"] != repo_id:
        raise ValueError("Memory bundle belongs to a different repository or kind")
    _text(repo_id, "repo_id", MAX_REPO_CHARS)
    records = bundle["records"]
    if not isinstance(records, list) or len(records) > MAX_BUNDLE_RECORDS:
        raise ValueError("Memory bundle contains too many records")
    indexed = {}
    for record in records:
        if not isinstance(record, dict) or set(record) != _FIELDS:
            raise ValueError("Unsupported feedback record schema")
        uid = record["uid"]
        if not isinstance(uid, str) or not re.fullmatch(r"[0-9a-f]{32}", uid) or uid in indexed:
            raise ValueError("Feedback UIDs must be unique 32-character hex strings")
        indexed[uid] = record
        for field, limit in (
            ("text", MAX_TEXT_CHARS),
            ("source", MAX_SOURCE_CHARS),
            ("path_glob", MAX_SCOPE_CHARS),
        ):
            if _text(record[field], field, limit) != record[field]:
                raise ValueError(f"{field} must not contain leading or trailing whitespace")
        scope = record["path_glob"]
        if scope != "*":
            _safe_path(scope)
        if not isinstance(record["disposition"], str) or record["disposition"] not in {
            "accepted",
            "dismissed",
        }:
            raise ValueError("Invalid feedback disposition")
        links = tuple(record[k] for k in ("source_run_id", "finding_id", "rule_key"))
        if _feedback_links(*links) != links:
            raise ValueError("Feedback links must not have surrounding whitespace")
        created = _timestamp(record["created_at"], "created_at")
        expires = _timestamp(record["expires_at"], "expires_at")
        if not timedelta(0) < expires - created <= timedelta(days=MAX_TTL_DAYS):
            raise ValueError("Feedback lifetime is invalid")
        if created > datetime.now(UTC) + timedelta(minutes=5):
            raise ValueError("Feedback creation time is in the future")
        status = record["status"]
        if not isinstance(status, str) or status not in {"active", "revoked", "superseded"}:
            raise ValueError("Invalid feedback status")
        if status == "active":
            if any(record[k] is not None for k in ("replaced_by", "status_reason", "updated_at")):
                raise ValueError("Active feedback must not have terminal status metadata")
        else:
            _text(record["status_reason"], "status_reason", MAX_SOURCE_CHARS)
            if _timestamp(record["updated_at"], "updated_at") < created:
                raise ValueError("Feedback update predates its creation")
            if status == "revoked" and record["replaced_by"] is not None:
                raise ValueError("Revoked feedback must not name a replacement")
            if status == "superseded" and not isinstance(record["replaced_by"], str):
                raise ValueError("Superseded feedback requires a replacement UID")
    for record in records:
        successor = record["replaced_by"]
        if successor is not None:
            if successor not in indexed or successor == record["uid"]:
                raise ValueError("Feedback replacement is missing or refers to itself")
            if indexed[successor]["created_at"] <= record["created_at"]:
                raise ValueError("Feedback replacements must be created after the original")
    return {
        **bundle,
        "records": sorted(records, key=lambda r: (r["created_at"], r["uid"])),
    }


def bundle_from_records(repo_id, records):
    uids = {r["id"]: r["record_uid"] for r in records}
    exported = []
    for record in records:
        fields = {k: record[k] for k in _FIELDS - {"uid", "replaced_by"}}
        fields.update(uid=record["record_uid"], replaced_by=uids.get(record["replaced_by"]))
        if record["replaced_by"] is not None and fields["replaced_by"] is None:
            raise ValueError("Feedback replacement belongs to a different repository")
        exported.append(fields)
    return validate_bundle(
        {
            "schema_version": 1,
            "kind": "pr-review-human-feedback",
            "repo_id": repo_id,
            "records": exported,
        },
        repo_id,
    )


def _unique_keys(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Memory bundle JSON contains duplicate keys")
        result[key] = value
    return result


def decode_bundle(content, repo_id):
    if not isinstance(content, bytes) or len(content) > MAX_BUNDLE_BYTES:
        raise ValueError("Memory bundle exceeds the supported size")
    try:
        value = json.loads(content, object_pairs_hook=_unique_keys)
    except (UnicodeError, json.JSONDecodeError, RecursionError):
        raise ValueError("Memory bundle must contain valid bounded UTF-8 JSON") from None
    return validate_bundle(value, repo_id)


def read_bundle(path: Path, repo_id):
    with path.open("rb") as stream:
        return decode_bundle(stream.read(MAX_BUNDLE_BYTES + 1), repo_id)


def load_github_memory(snapshot, source, client, store: MemoryStore, *, path=DEFAULT_MEMORY_PATH):
    """Pin the default branch, validate its file, then import before model use.

    This never reads feedback from the contributor's head or writes to GitHub.
    Enabled repositories require a valid file; missing/inaccessible data fails.
    """
    path = _safe_path(path)
    if not path.endswith(".json"):
        raise ValueError("GitHub memory path must point to a JSON file")
    full_name = f"{source['owner']}/{source['repository']}"
    if snapshot.repository_identity != f"github:{source['repository_id']}":
        raise ValueError("Snapshot is not bound to the memory GitHub repository")
    endpoint = f"/repos/{full_name}"
    metadata = client.request("GET", endpoint)
    if (
        not isinstance(metadata, dict)
        or type(metadata.get("id")) is not int
        or (metadata.get("id") != source["repository_id"])
        or not isinstance(metadata.get("full_name"), str)
        or (metadata.get("full_name", "").casefold() != full_name.casefold())
    ):
        raise ValueError("Memory repository differs from the reviewed repository")
    branch = metadata.get("default_branch")
    if not isinstance(branch, str) or not branch or len(branch) > 256:
        raise ValueError("GitHub returned an invalid default branch")
    commit_value = client.request("GET", f"{endpoint}/commits/{quote(branch, safe='')}")
    commit = commit_value.get("sha") if isinstance(commit_value, dict) else None
    if not isinstance(commit, str) or not re.fullmatch(r"[0-9a-f]{40}", commit):
        raise ValueError("GitHub returned an invalid memory commit SHA")
    value = client.request("GET", f"{endpoint}/contents/{quote(path, safe='/')}?ref={commit}")
    if (
        not isinstance(value, dict)
        or value.get("type") != "file"
        or (
            value.get("path") != path
            or value.get("encoding") != "base64"
            or value.get("submodule_git_url")
            or value.get("target")
        )
    ):
        raise ValueError("GitHub memory must be a bounded JSON file")
    size = value.get("size")
    encoded = value.get("content")
    if (
        type(size) is not int
        or not 0 <= size <= MAX_BUNDLE_BYTES
        or (not isinstance(encoded, str) or len(encoded) > 2 * MAX_BUNDLE_BYTES)
    ):
        raise ValueError("GitHub memory exceeds the supported size")
    try:
        content = base64.b64decode(encoded.replace("\n", ""), validate=True)
    except ValueError:
        raise ValueError("GitHub memory has invalid base64 content") from None
    if len(content) != size:
        raise ValueError("GitHub memory size does not match its content")
    blob = hashlib.sha1(f"blob {len(content)}\0".encode() + content).hexdigest()
    if value.get("sha") != blob:
        raise ValueError("GitHub memory blob SHA does not match its content")
    bundle = decode_bundle(content, snapshot.repo_id)
    origin = {
        "kind": "github-default-branch",
        "repository_id": source["repository_id"],
        "repository": full_name,
        "branch": branch,
        "commit_sha": commit,
        "path": path,
        "blob_sha": blob,
        "file_sha256": hashlib.sha256(content).hexdigest(),
    }
    store.import_bundle(snapshot.repo_id, bundle, origin=origin)
    return {**origin, "bundle_sha256": bundle_hash(bundle), "record_count": len(bundle["records"])}
