"""Optional bounded JSON transport for pure compilation results from trusted jobs."""

import hashlib
import json
import re
import sqlite3
import zipfile
from dataclasses import asdict
from pathlib import PurePosixPath

from pr_review_harness.checks import syntax_compiler_identity
from pr_review_harness.cloud_state import download_trusted_artifact
from pr_review_harness.persistence import atomic_json, digest

MAX_ENTRIES = 128
MAX_BYTES = 8_000_000
MAX_SOURCE_RUNS = 10


def cache_identity(directory):
    return {"enabled": True, "directory": str(directory.resolve())}


def _hex(value, length):
    return isinstance(value, str) and re.fullmatch(f"[0-9a-f]{{{length}}}", value) is not None


def validate_entry(entry, repo_id, compiler):
    if not isinstance(entry, dict) or set(entry) != {"key", "value"}:
        raise ValueError("Malformed syntax cache entry")
    value = entry["value"]
    if not isinstance(value, dict) or set(value) != {
        "status",
        "exit_code",
        "output",
        "run_id",
        "sha",
        "inputs",
    }:
        raise ValueError("Malformed syntax cache result")
    inputs = value["inputs"]
    if not isinstance(inputs, dict) or set(inputs) != {
        "kind",
        "repo_id",
        "path",
        "source_sha256",
        "compiler",
    }:
        raise ValueError("Malformed syntax cache inputs")
    path = inputs["path"]
    if (
        inputs["kind"] != "syntax"
        or inputs["repo_id"] != repo_id
        or inputs["compiler"] != compiler
        or not _hex(inputs["source_sha256"], 64)
        or not isinstance(path, str)
        or not path.endswith(".py")
        or len(path) > 1024
        or PurePosixPath(path).is_absolute()
        or ".." in PurePosixPath(path).parts
        or any(ord(c) < 32 for c in path)
        or "\\" in path
        or not _hex(entry["key"], 64)
        or digest(inputs) != entry["key"]
    ):
        raise ValueError("Syntax cache input identity differs")
    if (
        not isinstance(value["status"], str)
        or value["status"] not in {"passed", "failed"}
        or type(value["exit_code"]) is not int
        or value["exit_code"] != (0 if value["status"] == "passed" else 1)
        or not isinstance(value["output"], str)
        or len(value["output"]) > 8000
        or not isinstance(value["run_id"], str)
        or not re.fullmatch(r"[a-zA-Z0-9_-]{1,80}", value["run_id"])
        or not _hex(value["sha"], 40)
    ):
        raise ValueError("Syntax cache result is not reusable")


def export_syntax_cache(store, context, repo_id, output):
    compiler = syntax_compiler_identity()
    entries = []
    for entry in store.recent_checks(MAX_ENTRIES):
        entry["value"].pop("origin_artifact", None)
        try:
            validate_entry(entry, repo_id, compiler)
        except ValueError:
            continue
        entries.append(entry)
    bundle = {
        "schema_version": 1,
        "producer": asdict(context),
        "repo_id": repo_id,
        "compiler": compiler,
        "entries": entries,
    }
    if len(json.dumps(bundle, ensure_ascii=False, indent=2).encode()) > MAX_BYTES:
        raise ValueError("Syntax cache export exceeds size limit")
    atomic_json(output, bundle)
    return {
        "status": "saved",
        "entries": len(entries),
        "sha256": hashlib.sha256(output.read_bytes()).hexdigest(),
    }


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate syntax cache JSON key")
        result[key] = value
    return result


def import_syntax_cache(raw, store, context, origin, repo_id):
    if len(raw) > MAX_BYTES:
        raise ValueError("Syntax cache exceeds size limit")
    bundle = json.loads(raw, object_pairs_hook=_unique_object)
    compiler = syntax_compiler_identity()
    if not isinstance(bundle, dict) or set(bundle) != {
        "schema_version",
        "producer",
        "repo_id",
        "compiler",
        "entries",
    }:
        raise ValueError("Malformed syntax cache bundle")
    producer = bundle["producer"]
    if (
        type(bundle["schema_version"]) is not int
        or bundle["schema_version"] != 1
        or bundle["repo_id"] != repo_id
        or bundle["compiler"] != compiler
        or not isinstance(producer, dict)
        or set(producer) != set(asdict(context))
        or any(
            producer[key] != getattr(context, key)
            for key in (
                "repository",
                "repository_id",
                "workflow_path",
                "workflow_ref",
                "harness_sha",
            )
        )
        or producer["run_id"] != origin["run_id"]
        or producer["attempt"] != origin["attempt"]
        or producer["event"] != origin["event"]
        or not _hex(producer["workflow_sha"], 40)
        or (
            origin["event"] == "workflow_dispatch"
            and producer["workflow_sha"] != origin["head_sha"]
        )
        or not isinstance(bundle["entries"], list)
        or len(bundle["entries"]) > MAX_ENTRIES
    ):
        raise ValueError("Syntax cache producer, compiler or repository differs")
    keys = set()
    # Validate the entire batch before mutating SQLite; no partial poisoned imports.
    for entry in bundle["entries"]:
        validate_entry(entry, repo_id, compiler)
        if entry["key"] in keys:
            raise ValueError("Duplicate syntax cache entry")
        keys.add(entry["key"])
    for entry in bundle["entries"]:
        entry["value"]["origin_artifact"] = origin
    store.import_checks(bundle["entries"])
    return len(keys)


def restore_latest_syntax_cache(client, context, repo_id, store):
    """Try a bounded recent window; optimization failure never substitutes evidence."""
    rejected = 0
    try:
        endpoint = f"/repos/{context.repository}/actions/workflows/"
        runs = client.request(
            "GET",
            endpoint
            + context.workflow_path.rsplit("/", 1)[1]
            + f"/runs?status=completed&per_page={MAX_SOURCE_RUNS}",
        )["workflow_runs"][:MAX_SOURCE_RUNS]
        for run in runs:
            if run["id"] == context.run_id or run.get("conclusion") != "success":
                continue
            try:
                raw, origin = download_trusted_artifact(
                    client,
                    context,
                    (run["id"], run["run_attempt"]),
                    prefix="harness-syntax",
                    member_name="syntax-cache.json",
                    max_bytes=MAX_BYTES,
                )
                count = import_syntax_cache(raw, store, context, origin, repo_id)
            except (ValueError, RuntimeError, OSError, KeyError, TypeError, zipfile.BadZipFile):
                rejected += 1
                continue
            if count:
                return {
                    "status": "imported",
                    "entries": count,
                    "origin": origin,
                    "rejected_sources": rejected,
                }
        return {"status": "cold", "entries": 0, "rejected_sources": rejected}
    except (
        ValueError,
        RuntimeError,
        OSError,
        KeyError,
        TypeError,
        zipfile.BadZipFile,
        sqlite3.Error,
    ):
        return {"status": "unavailable", "entries": 0, "rejected_sources": rejected}
