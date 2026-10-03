"""Archive quiescent runs and recover only artifacts from a trusted Actions workflow."""

import hashlib
import io
import json
import os
import re
import shutil
import sqlite3
import stat
import subprocess
import tempfile
import zipfile
from contextlib import closing
from dataclasses import asdict, dataclass
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request

from pr_review_harness.github import PRRef
from pr_review_harness.persistence import RunStore, atomic_json, digest

MAX_ARCHIVE_BYTES = 32_000_000
MAX_STATE_BYTES = 128_000_000
MAX_JSON_BYTES = 16_000_000
STATE_FILES = {
    "manifest.json",
    "artifacts.json",
    "checkpoint.sqlite3",
    "executions.sqlite3",
    "review.json",
    "failed.json",
}
REQUIRED_FILES = {"manifest.json", "artifacts.json", "checkpoint.sqlite3", "executions.sqlite3"}


def _positive(value, label):
    value = str(value)
    if not re.fullmatch(r"[1-9][0-9]{0,17}", value):
        raise ValueError(f"Invalid {label}: expected a positive integer")
    return int(value)


def _sha(value, label):
    if not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{40}", value):
        raise ValueError(f"Invalid {label}: expected a commit SHA")
    return value


@dataclass(frozen=True)
class CloudContext:
    repository: str
    repository_id: int
    run_id: int
    attempt: int
    event: str
    workflow_path: str
    workflow_ref: str
    workflow_sha: str
    harness_sha: str

    @classmethod
    def from_environment(cls, repository, repository_id):
        ref = PRRef.parse(f"https://github.com/{repository}/pull/1")
        if os.getenv("GITHUB_REPOSITORY", "").casefold() != ref.full_name.casefold():
            raise ValueError("Cloud state requires the configured Actions repository")
        if _positive(os.getenv("GITHUB_REPOSITORY_ID", ""), "repository ID") != repository_id:
            raise ValueError("Cloud state repository identity differs from the event")
        workflow_ref = os.getenv("GITHUB_WORKFLOW_REF", "")
        prefix = ref.full_name + "/"
        if not workflow_ref.startswith(prefix) or "@refs/heads/" not in workflow_ref:
            raise ValueError("Cloud state requires a trusted branch workflow reference")
        path, branch = workflow_ref[len(prefix) :].split("@", 1)
        if not re.fullmatch(r"\.github/workflows/[A-Za-z0-9_.-]+\.ya?ml", path):
            raise ValueError("Invalid Actions workflow path")
        event = os.getenv("GITHUB_EVENT_NAME")
        if event not in {"workflow_dispatch", "pull_request_target"}:
            raise ValueError("Cloud state requires a trusted dispatch or PR target workflow")
        harness_sha = subprocess.check_output(
            ["git", "rev-parse", "HEAD"], text=True, timeout=10
        ).strip()
        return cls(
            ref.full_name,
            repository_id,
            _positive(os.getenv("GITHUB_RUN_ID", ""), "Actions run ID"),
            _positive(os.getenv("GITHUB_RUN_ATTEMPT", ""), "Actions attempt"),
            event,
            path,
            branch,
            _sha(os.getenv("GITHUB_WORKFLOW_SHA"), "workflow SHA"),
            _sha(harness_sha, "Harness SHA"),
        )


def resume_selection(context, run_id=None, attempt="1"):
    # Re-run may delete the previous attempt's artifacts before this job starts.
    # Require a new workflow run so the selected source stays independently addressable.
    if context.attempt > 1:
        raise ValueError(
            "Actions Re-run cannot reliably retain checkpoint artifacts; "
            "start a new Run workflow and select a completed source run"
        )
    if run_id:
        selected = _positive(run_id, "source run ID"), _positive(attempt, "source attempt")
        if selected[0] == context.run_id:
            raise ValueError("Cannot recover the current in-progress Actions run")
        return selected
    return None


def state_profile(arguments):
    return {
        "verify": arguments.verify,
        "verify_max_findings": arguments.verify_max_findings,
        "no_memory": arguments.no_memory,
        "github_memory": arguments.github_memory,
        "github_memory_path": arguments.github_memory_path,
    }


def write_pointer(path, context, runs_dir, run_id, profile, restored_from=None):
    atomic_json(
        path,
        {
            "schema_version": 1,
            "producer": asdict(context),
            "runs_dir": str(runs_dir.resolve()),
            "run_id": run_id,
            "profile": profile,
            "restored_from": restored_from,
        },
    )


def export_checkpoint(pointer_path, output, context):
    if not pointer_path.exists():
        return {"status": "skipped", "reason": "no_cloud_run_pointer"}
    pointer = json.loads(pointer_path.read_text())
    if pointer.get("schema_version") != 1 or pointer.get("producer") != asdict(context):
        raise ValueError("Checkpoint producer differs from this Actions attempt")
    root = Path(pointer["runs_dir"])
    run_id = pointer["run_id"]
    if not (root / run_id / "manifest.json").exists():
        return {"status": "skipped", "reason": "no_initialized_run"}
    store = RunStore(root, run_id)
    output.parent.mkdir(parents=True, exist_ok=True)
    with store.locked(), tempfile.TemporaryDirectory(dir=output.parent) as directory:
        staging = Path(directory)
        manifest, _ = store.load()
        if manifest["run_tests"] or manifest["incremental"].get("enabled"):
            raise ValueError("Cloud checkpoints support syntax-only, non-incremental reviews")
        if manifest["source"]["repository_id"] != context.repository_id:
            raise ValueError("Checkpoint belongs to another repository")
        for name in STATE_FILES:
            source = store.path / name
            if not source.exists():
                continue
            if source.is_symlink() or not source.is_file():
                raise ValueError("Checkpoint files must be regular files")
            if name.endswith(".sqlite3"):
                # Copy committed pages, including WAL, while no reviewer holds the run lock.
                with closing(sqlite3.connect(source.as_uri() + "?mode=ro", uri=True)) as original:
                    with closing(sqlite3.connect(staging / name)) as backup:
                        original.backup(backup)
            else:
                shutil.copyfile(source, staging / name)
        files = {p.name: p for p in staging.iterdir()}
        if not REQUIRED_FILES <= files.keys():
            raise ValueError("No complete resumable checkpoint exists")
        if sum(p.stat().st_size for p in files.values()) > MAX_STATE_BYTES:
            raise ValueError("Cloud checkpoint exceeds state size limit")
        if any(p.suffix == ".json" and p.stat().st_size > MAX_JSON_BYTES for p in files.values()):
            raise ValueError("Checkpoint JSON exceeds size limit")
        metadata = {
            **pointer,
            "files": {
                name: {
                    "bytes": p.stat().st_size,
                    "sha256": hashlib.sha256(p.read_bytes()).hexdigest(),
                }
                for name, p in sorted(files.items())
            },
            "budget_usage": store.budget_usage(),
        }
        atomic_json(staging / "cloud-state.json", metadata)
        target = staging / "checkpoint.zip"
        with zipfile.ZipFile(target, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            for name in sorted([*files, "cloud-state.json"]):
                archive.write(staging / name, name)
        if target.stat().st_size > MAX_ARCHIVE_BYTES:
            raise ValueError("Cloud checkpoint exceeds archive size limit")
        target.chmod(0o600)
        os.replace(target, output)
    return {
        "status": "saved",
        "run_id": run_id,
        "sha256": hashlib.sha256(output.read_bytes()).hexdigest(),
        "budget_usage": metadata["budget_usage"],
    }


def _download(client, endpoint):
    headers = {"User-Agent": "pr-review-harness", "Accept": "application/vnd.github+json"}
    if client.token:
        headers["Authorization"] = f"Bearer {client.token}"
    request = Request("https://api.github.com" + endpoint, headers=headers)
    try:
        try:
            response = client.opener.open(request, timeout=30)
        except HTTPError as exc:
            if exc.code != 302:
                raise RuntimeError(f"GitHub artifact download HTTP {exc.code}") from None
            location = exc.headers.get("Location", "")
            parsed = urlsplit(location)
            if (
                parsed.scheme != "https"
                or parsed.username
                or parsed.password
                or parsed.port not in (None, 443)
                or not parsed.hostname
                or not parsed.hostname.endswith(
                    (".blob.core.windows.net", ".actions.githubusercontent.com")
                )
            ):
                raise ValueError("Unexpected artifact download host") from None
            # Signed storage URL receives no GitHub authentication header; no further redirects.
            response = client.opener.open(
                Request(location, headers={"User-Agent": "pr-review-harness"}), timeout=30
            )
        with response:
            data = response.read(MAX_ARCHIVE_BYTES + 1)
    except (URLError, TimeoutError) as exc:
        raise RuntimeError("Artifact download failed") from exc
    if len(data) > MAX_ARCHIVE_BYTES:
        raise ValueError("Artifact download exceeds archive size limit")
    return data


def download_checkpoint(client, context, selection):
    run_id, attempt = selection
    endpoint = f"/repos/{context.repository}"
    repository = client.request("GET", endpoint)
    if repository["id"] != context.repository_id:
        raise ValueError("Recovery repository identity differs from GitHub")
    if context.workflow_ref != "refs/heads/" + repository["default_branch"]:
        raise ValueError("Recovery must run the workflow on the trusted default branch")
    workflow = client.request(
        "GET", endpoint + "/actions/workflows/" + context.workflow_path.rsplit("/", 1)[1]
    )
    run = client.request("GET", endpoint + f"/actions/runs/{run_id}/attempts/{attempt}")
    if (
        run["id"] != run_id
        or run["run_attempt"] != attempt
        or run["status"] != "completed"
        or run["repository"]["id"] != context.repository_id
        or run["workflow_id"] != workflow["id"]
        or run["path"] != context.workflow_path
        or run["event"] not in {"workflow_dispatch", "pull_request_target"}
    ):
        raise ValueError("Recovery source is not this repository's trusted completed workflow")
    if run["event"] == "workflow_dispatch" and (
        run["head_branch"] != repository["default_branch"]
        or run["head_repository"]["id"] != context.repository_id
    ):
        raise ValueError("Recovery dispatch source must be on the trusted default branch")
    name = f"harness-state-{run_id}-{attempt}"
    listing = client.request("GET", endpoint + f"/actions/artifacts?name={name}&per_page=100")
    if listing["total_count"] > 100:
        raise ValueError("Too many checkpoint artifacts with the same name")
    matches = [item for item in listing["artifacts"] if item["name"] == name]
    if len(matches) != 1:
        raise ValueError("No unique saved checkpoint exists for this Actions attempt")
    artifact = matches[0]
    if (
        artifact["expired"]
        or artifact["workflow_run"]["id"] != run_id
        or artifact["workflow_run"]["repository_id"] != context.repository_id
        or artifact["size_in_bytes"] > MAX_ARCHIVE_BYTES
    ):
        raise ValueError("Checkpoint artifact is expired, oversized or belongs to another run")
    data = _download(
        client, endpoint + f"/actions/artifacts/{_positive(artifact['id'], 'artifact ID')}/zip"
    )
    if artifact.get("digest") != "sha256:" + hashlib.sha256(data).hexdigest():
        raise ValueError("GitHub checkpoint artifact digest differs from the download")
    # upload-artifact wraps the one application bundle in its own ZIP.
    with zipfile.ZipFile(io.BytesIO(data)) as outer:
        if outer.namelist() != ["checkpoint.zip"]:
            raise ValueError("Unexpected checkpoint artifact members")
        member = outer.getinfo("checkpoint.zip")
        if member.file_size > MAX_ARCHIVE_BYTES:
            raise ValueError("Checkpoint bundle exceeds archive size limit")
        bundle = outer.read(member)
    return bundle, {
        "run_id": run_id,
        "attempt": attempt,
        "event": run["event"],
        "head_sha": run["head_sha"],
        "artifact_id": artifact["id"],
        "artifact_digest": artifact["digest"],
    }


def restore_checkpoint(
    bundle, runs_dir, context, origin, snapshot, expected, profile, current_source
):
    """Validate JSON and all member identities before making serialized checkpoints available."""
    if len(bundle) > MAX_ARCHIVE_BYTES:
        raise ValueError("Checkpoint bundle exceeds archive size limit")
    with zipfile.ZipFile(io.BytesIO(bundle)) as archive:
        names = archive.namelist()
        if len(names) != len(set(names)) or not REQUIRED_FILES <= set(names):
            raise ValueError("Checkpoint has duplicate or missing state members")
        if not set(names) <= STATE_FILES | {"cloud-state.json"} or "cloud-state.json" not in names:
            raise ValueError("Unexpected checkpoint state members")
        if sum(item.file_size for item in archive.infolist()) > MAX_STATE_BYTES:
            raise ValueError("Checkpoint exceeds state size limit")
        for item in archive.infolist():
            if item.flag_bits & 1 or stat.S_ISLNK(item.external_attr >> 16):
                raise ValueError("Encrypted or symbolic checkpoint members are not supported")
            if item.filename.endswith(".json") and item.file_size > MAX_JSON_BYTES:
                raise ValueError("Checkpoint JSON exceeds size limit")
        metadata = json.loads(archive.read("cloud-state.json"))
        manifest = json.loads(archive.read("manifest.json"))
        artifacts = json.loads(archive.read("artifacts.json"))
        if (
            not isinstance(metadata, dict)
            or not {"producer", "profile", "run_id", "runs_dir", "files", "budget_usage"}
            <= metadata.keys()
            or not isinstance(metadata["producer"], dict)
            or not CloudContext.__annotations__.keys() <= metadata["producer"].keys()
            or not isinstance(metadata["files"], dict)
            or not isinstance(manifest, dict)
            or not {"run_id", "artifacts_sha256", "source"} <= manifest.keys()
            or not isinstance(manifest["source"], dict)
            or not {"repository_id", "head_sha", "base_sha"} <= manifest["source"].keys()
            or not isinstance(artifacts, dict)
        ):
            raise ValueError("Malformed checkpoint metadata or frozen state")
        producer = metadata["producer"]
        if (
            metadata.get("schema_version") != 1
            or not isinstance(producer["repository"], str)
            or producer["repository"].casefold() != context.repository.casefold()
            or producer["repository_id"] != context.repository_id
            or producer["run_id"] != origin["run_id"]
            or producer["attempt"] != origin["attempt"]
            or producer["event"] != origin["event"]
            or producer["workflow_path"] != context.workflow_path
            or producer["workflow_ref"] != context.workflow_ref
            or producer["harness_sha"] != context.harness_sha
            or (
                origin["event"] == "workflow_dispatch"
                and producer["workflow_sha"] != origin["head_sha"]
            )
        ):
            raise ValueError(
                "Checkpoint producer or Harness commit differs from the trusted origin"
            )
        if metadata["profile"] != profile:
            raise ValueError("Cloud review configuration changed; start a fresh run")
        run_id = metadata["run_id"]
        if not isinstance(run_id, str) or not re.fullmatch(r"[a-zA-Z0-9_-]{1,80}", run_id):
            raise ValueError("Invalid checkpoint run identity")
        if metadata["runs_dir"] != str(runs_dir.resolve()):
            raise ValueError("Cloud recovery requires the same workspace and run directory")
        if manifest["run_id"] != run_id or digest(artifacts) != manifest["artifacts_sha256"]:
            raise ValueError("Run identity or frozen artifacts have changed")
        changed = [key for key, value in expected.items() if manifest.get(key) != value]
        if changed:
            raise ValueError("Cannot resume: changed identity fields: " + ", ".join(changed))
        source = manifest["source"]
        if (
            source["repository_id"] != context.repository_id
            or source["head_sha"] != snapshot.head_sha
            or source["base_sha"] != snapshot.base_sha
            or any(
                source.get(key) != current_source.get(key)
                for key in (
                    "kind",
                    "url",
                    "repository_id",
                    "owner",
                    "repository",
                    "number",
                    "state",
                )
            )
        ):
            raise ValueError("PR identity or versions changed; start a fresh review")
        if set(metadata["files"]) != set(names) - {"cloud-state.json"}:
            raise ValueError("Checkpoint file manifest differs from archive members")
        for name, info in metadata["files"].items():
            if not isinstance(info, dict) or not {"bytes", "sha256"} <= info.keys():
                raise ValueError("Malformed checkpoint file identity")
            raw = archive.read(name)
            if len(raw) != info["bytes"] or hashlib.sha256(raw).hexdigest() != info["sha256"]:
                raise ValueError("Checkpoint member digest differs: " + name)
        destination = runs_dir.resolve() / run_id
        if destination.exists():
            raise ValueError("Refusing to overwrite an existing run directory")
        runs_dir.mkdir(parents=True, exist_ok=True)
        staging = Path(tempfile.mkdtemp(prefix=".restore-", dir=runs_dir))
        try:
            for name in metadata["files"]:
                target = staging / name
                target.write_bytes(archive.read(name))
                target.chmod(0o600)
            os.replace(staging, destination)
        finally:
            if staging.exists():
                shutil.rmtree(staging)
    return manifest, {**origin, "harness_run_id": run_id, "budget_usage": metadata["budget_usage"]}
