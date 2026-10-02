"""Validate GitHub events before spending model budget or publishing a review."""

import re
from dataclasses import dataclass

from pr_review_harness.github import PRRef


@dataclass(frozen=True)
class ReviewEvent:
    ref: PRRef | None
    repository_id: int
    head_sha: str | None = None
    base_sha: str | None = None
    skip_reason: str | None = None

    def validate_source(self, source):
        if source["repository_id"] != self.repository_id:
            raise ValueError("Event repository identity differs from GitHub")
        for side in ("head", "base"):
            expected = getattr(self, side + "_sha")
            if expected and source[side + "_sha"] != expected:
                raise ValueError("PR changed since this event; wait for the current event")
        if source["state"] != "open":
            raise ValueError("PR is no longer open")


def parse_event(value, *, repository, event_name):
    """Only a trusted Actions environment chooses the repository/event type.

    PR titles, branch names, head repository URLs and arbitrary dispatch URLs
    are never used to select a transport or form a shell command.
    """
    if not isinstance(value, dict) or not isinstance(value.get("repository"), dict):
        raise ValueError("Expected a GitHub event object with repository metadata")
    probe = PRRef.parse(f"https://github.com/{repository}/pull/1")
    repo = value.get("repository") or {}
    repo_id = repo.get("id")
    if repo.get("full_name", "").casefold() != probe.full_name.casefold() or (
        type(repo_id) is not int or repo_id < 1
    ):
        raise ValueError("Event repository does not match the configured Actions repository")
    if event_name == "workflow_dispatch":
        number = str((value.get("inputs") or {}).get("pr_number", ""))
        if not re.fullmatch(r"[1-9][0-9]{0,8}", number):
            raise ValueError("Dispatch requires a positive pr_number")
        return ReviewEvent(PRRef(probe.owner, probe.repository, int(number)), repo_id)
    if event_name != "pull_request_target":
        raise ValueError("Only pull_request_target and workflow_dispatch are supported")
    if value.get("action") not in {"opened", "reopened", "synchronize", "ready_for_review"}:
        return ReviewEvent(None, repo_id, skip_reason="unsupported_action")
    pr = value.get("pull_request") or {}
    number = value.get("number")
    if type(number) is not int or not 1 <= number <= 999999999 or pr.get("number") != number:
        raise ValueError("Event contains an invalid PR number")
    base_repo = (pr.get("base") or {}).get("repo") or {}
    if base_repo.get("id") != repo_id or (
        base_repo.get("full_name", "").casefold() != probe.full_name.casefold()
    ):
        raise ValueError("PR base repository differs from the Actions repository")
    if pr.get("state") != "open" or pr.get("draft") is True:
        return ReviewEvent(None, repo_id, skip_reason="closed_or_draft")
    shas = {}
    for side in ("base", "head"):
        sha = (pr.get(side) or {}).get("sha")
        if not isinstance(sha, str) or not re.fullmatch(r"[0-9a-f]{40}", sha):
            raise ValueError("Event contains an invalid commit SHA")
        shas[side + "_sha"] = sha
    return ReviewEvent(PRRef(probe.owner, probe.repository, number), repo_id, **shas)
