"""GitHub PR input and previewable, SHA-bound COMMENT publication."""

import base64
import json
import os
import re
import subprocess
from dataclasses import asdict, dataclass
from pathlib import Path
from types import SimpleNamespace
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

from pr_review_harness.models import Finding
from pr_review_harness.persistence import RunStore, digest
from pr_review_harness.runtime import validate_findings
from pr_review_harness.snapshot import Snapshot

API_VERSION = "2026-03-10"


@dataclass(frozen=True)
class PRRef:
    owner: str
    repository: str
    number: int

    @classmethod
    def parse(cls, value):
        parsed = urlsplit(value)
        match = re.fullmatch(
            r"/([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+)/pull/([1-9][0-9]*)/?", parsed.path
        )
        if (
            parsed.scheme != "https"
            or parsed.netloc != "github.com"
            or not match
            or (parsed.query or parsed.fragment)
        ):
            raise ValueError("Use a PR URL like https://github.com/OWNER/REPO/pull/123")
        owner, repository, number = match.groups()
        if owner in {".", ".."} or repository in {".", ".."}:
            raise ValueError("Invalid GitHub repository name")
        return cls(owner, repository, int(number))

    @property
    def full_name(self):
        return f"{self.owner}/{self.repository}"

    @property
    def endpoint(self):
        return f"/repos/{self.full_name}/pulls/{self.number}"

    @property
    def url(self):
        return f"https://github.com/{self.full_name}/pull/{self.number}"


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None  # Never forward an authentication header to another endpoint.


class GitHubClient:
    def __init__(self, token=None):
        self.token = token
        self.opener = build_opener(_NoRedirect())

    def request(self, method, endpoint, payload=None):
        if not endpoint.startswith("/") or endpoint.startswith("//"):
            raise ValueError("Expected a GitHub API path")
        headers = {
            "Accept": "application/vnd.github+json",
            "User-Agent": "pr-review-harness",
            "X-GitHub-Api-Version": API_VERSION,
        }
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        data = None if payload is None else json.dumps(payload).encode()
        if data is not None:
            headers["Content-Type"] = "application/json"
        request = Request(
            "https://api.github.com" + endpoint, data=data, headers=headers, method=method
        )
        try:
            with self.opener.open(request, timeout=30) as response:
                body = response.read(8_000_001)
        except HTTPError as exc:
            raise RuntimeError(
                f"GitHub API HTTP {exc.code}; check URL, permissions and rate limit"
            ) from None
        except (URLError, TimeoutError) as exc:
            raise RuntimeError("GitHub API connection failed") from exc
        if len(body) > 8_000_000:
            raise ValueError("GitHub response exceeds supported size")
        return json.loads(body)

    def pages(self, endpoint):
        rows = []
        for page in range(1, 11):
            batch = self.request("GET", f"{endpoint}?per_page=100&page={page}")
            if not isinstance(batch, list):
                raise ValueError("Expected a paginated GitHub list")
            rows.extend(batch)
            if len(batch) < 100:
                return rows
        raise ValueError("Too many GitHub reviews to safely check for duplicates")

    def pull_request(self, ref):
        value = self.request("GET", ref.endpoint)
        repository = value["base"]["repo"]
        if repository["full_name"].casefold() != ref.full_name.casefold() or (
            value["number"] != ref.number
        ):
            raise ValueError("GitHub returned a different repository or PR")
        for side in ("base", "head"):
            if not re.fullmatch(r"[0-9a-f]{40}", value[side]["sha"]):
                raise ValueError("GitHub returned an invalid commit SHA")
        if type(repository["id"]) is not int or repository["id"] < 1:
            raise ValueError("GitHub returned an invalid repository ID")
        return {
            "kind": "github",
            "url": ref.url,
            "repository_id": repository["id"],
            "owner": ref.owner,
            "repository": ref.repository,
            "number": ref.number,
            "base_sha": value["base"]["sha"],
            "head_sha": value["head"]["sha"],
            "state": value["state"],
            "title": value["title"],
        }


def configured_client():
    token = os.getenv("HARNESS_GITHUB_TOKEN") or os.getenv("GH_TOKEN") or os.getenv("GITHUB_TOKEN")
    if not token:
        try:
            result = subprocess.run(
                ["gh", "auth", "token", "--hostname", "github.com"],
                capture_output=True,
                text=True,
                timeout=10,
            )
            if result.returncode == 0:
                token = result.stdout.strip()
        except (OSError, subprocess.TimeoutExpired):
            pass
    return GitHubClient(token)


def _git(repo, token, *args):
    env = {
        k: os.environ[k]
        for k in ("PATH", "LANG", "SYSTEMROOT", "SSL_CERT_FILE", "SSL_CERT_DIR")
        if k in os.environ
    }
    env.update(
        GIT_TERMINAL_PROMPT="0",
        GIT_CONFIG_GLOBAL=os.devnull,
        GIT_CONFIG_NOSYSTEM="1",
        GIT_CONFIG_COUNT="0",
    )
    if token:
        auth = base64.b64encode(f"x-access-token:{token}".encode()).decode()
        env.update(
            GIT_CONFIG_COUNT="1",
            GIT_CONFIG_KEY_0="http.https://github.com/.extraheader",
            GIT_CONFIG_VALUE_0=f"Authorization: Basic {auth}",
        )
    try:
        result = subprocess.run(
            [
                "git",
                "-c",
                "core.hooksPath=" + os.devnull,
                "-c",
                "protocol.file.allow=never",
                "-C",
                str(repo),
                *args,
            ],
            capture_output=True,
            env=env,
            timeout=180,
        )
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError("GitHub Git fetch timed out") from exc
    if result.returncode:
        # Avoid reflecting credential helpers or token-bearing transport messages.
        raise RuntimeError("GitHub Git operation failed; check repository access and network")


def fetch_snapshot(ref, client, cache_dir):
    source = client.pull_request(ref)
    root = Path(cache_dir).resolve()
    key = "repository-" + str(source["repository_id"])
    with RunStore(root / "locks", key).locked():
        repo = root / key
        repo.mkdir(parents=True, exist_ok=True)
        if not (repo / ".git").exists():
            _git(repo, client.token, "init", "--template=")
        remote = f"https://github.com/{ref.full_name}.git"
        # Exact commits with their ancestors; shallow fetch can select a wrong merge base.
        _git(
            repo,
            client.token,
            "fetch",
            "--no-tags",
            "--no-recurse-submodules",
            remote,
            source["base_sha"],
            source["head_sha"],
        )
        current = client.pull_request(ref)
        if any(current[k] != source[k] for k in ("repository_id", "base_sha", "head_sha")):
            raise ValueError("PR versions changed during fetch; start a new review")
        snapshot = Snapshot.load(
            repo,
            source["base_sha"],
            source["head_sha"],
            repository_identity=f"github:{source['repository_id']}",
        )
        return snapshot, source


def publication_plan(report, client):
    source = report.get("source") or {}
    if (
        source.get("kind") != "github"
        or report.get("mode") != "live"
        or (report.get("submission", {}).get("outcome") != "submitted")
    ):
        raise ValueError("Publication requires a completed live github-review report")
    ref = PRRef.parse(source["url"])
    current = client.pull_request(ref)
    if current["state"] != "open":
        raise ValueError("PR is closed; publication refused")
    if any(current[k] != source[k] for k in ("repository_id", "base_sha", "head_sha")) or (
        source["base_sha"] != report["base_sha"] or source["head_sha"] != report["head_sha"]
    ):
        raise ValueError("PR base/head changed; review the current versions before publishing")
    snapshot = Snapshot.load(
        Path(report["repo"]),
        report["base_sha"],
        report["head_sha"],
        repository_identity=f"github:{source['repository_id']}",
    )
    if snapshot.repo_id != report["repo_id"] or snapshot.merge_base_sha != report["merge_base_sha"]:
        raise ValueError("Report repository or merge base does not match its snapshot")
    findings = [Finding(**{k: v for k, v in f.items() if k != "id"}) for f in report["findings"]]
    findings, errors = validate_findings(
        snapshot, findings, [SimpleNamespace(id=v["id"]) for v in report["evidence"]]
    )
    if errors:
        raise ValueError("Report has invalid finding locations or evidence IDs")
    key = digest(
        {
            "repository_id": source["repository_id"],
            "number": ref.number,
            "head_sha": snapshot.head_sha,
            "merge_base_sha": snapshot.merge_base_sha,
        }
    )
    marker = f"<!-- pr-review-harness:{key} -->"
    comments = [
        {
            "path": f.path,
            "line": f.line,
            "side": "RIGHT",
            "body": f"**[{f.severity}] {f.title}**\n\n{f.explanation}\n\n"
            f"Trigger: {f.trigger}\n\nEvidence: {', '.join(f.evidence_ids) or 'not executed'}; "
            f"model confidence: {f.confidence}. Please verify this suggestion.",
        }
        for f in findings
    ]
    body = (
        "Automated PR review suggestions for maintainer verification. "
        "This review does not approve or reject the PR.\n\n"
        f"Findings: {len(comments)}. Merge base: `{snapshot.merge_base_sha}`. "
        "An empty review does not establish the absence of defects.\n\n" + marker
    )
    return {
        "schema_version": 1,
        "pr": asdict(ref),
        "url": ref.url,
        "key": key,
        "marker": marker,
        "payload": {
            "commit_id": snapshot.head_sha,
            "event": "COMMENT",
            "body": body,
            "comments": comments,
        },
    }


def publish_report(report, client, publications_dir, *, send=False, retry_unknown=False):
    plan = publication_plan(report, client)
    if not send:
        return {"status": "preview", **plan}
    if not client.token:
        raise ValueError("GitHub authentication is required to publish")
    ref = PRRef(**plan["pr"])
    store = RunStore(publications_dir, plan["key"])
    with store.locked():
        receipt = store.receipt("publication")
        if receipt and receipt["status"] == "completed":
            return {"status": "already_published", **receipt["payload"], "plan": plan}
        user = client.request("GET", "/user")
        reviews = client.pages(ref.endpoint + "/reviews")
        existing = next(
            (
                r
                for r in reviews
                if plan["marker"] in (r.get("body") or "")
                and (
                    r.get("user", {}).get("id") == user["id"]
                    and r.get("commit_id") == report["head_sha"]
                    and r.get("state") == "COMMENTED"
                )
            ),
            None,
        )
        if existing:
            if not receipt:
                store.start("publication", "github_review")
            value = {"review_id": existing["id"], "url": existing["html_url"]}
            store.finish("publication", value)
            return {"status": "already_published", **value, "plan": plan}
        if receipt and not retry_unknown:
            raise ValueError(
                "Publication execution_unknown: remote result not found. "
                "Inspect GitHub before using --retry-unknown"
            )
        # Recheck after pagination and immediately before the irreversible request.
        publication_plan(report, client)
        if not receipt:
            store.start("publication", "github_review")
        response = client.request("POST", ref.endpoint + "/reviews", plan["payload"])
        value = {"review_id": response["id"], "url": response["html_url"]}
        store.finish("publication", value)
        return {"status": "published", **value, "plan": plan}
