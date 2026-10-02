"""Create SHA-pinned cases and separate gold labels for public PR evaluation."""

from __future__ import annotations

import hashlib
import json
import re
import subprocess
from pathlib import Path

from pr_review_harness.snapshot import Snapshot

PROJECT = Path(__file__).resolve().parents[2]
SOURCE = Path(__file__).with_name("manifest.json")
OUTPUT = PROJECT / ".pr-harness" / "evaluation" / "independent-real-prs"


def _git(*args: str, cwd: Path | None = None) -> str:
    result = subprocess.run(
        ["git", *args], cwd=cwd, capture_output=True, text=True, check=True, timeout=180
    )
    return result.stdout.strip()


def prepare() -> Path:
    source = json.loads(SOURCE.read_text(encoding="utf-8"))
    repositories = source.get("repositories")
    cases = source.get("cases")
    if source.get("schema_version") != 1 or not isinstance(repositories, dict):
        raise ValueError("Expected a version 1 repository manifest")
    if not isinstance(cases, list) or not cases:
        raise ValueError("Expected a nonempty case list")
    if len({case["id"] for case in cases}) != len(cases):
        raise ValueError("Duplicate case ID")
    groups: dict[str, set[str]] = {}
    for case in cases:
        group = case["root_cause_group"]
        role = case["case_role"]
        if not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", group):
            raise ValueError(f"Invalid root cause group: {group}")
        if role not in {"regression", "fix", "control"} or role in groups.setdefault(group, set()):
            raise ValueError(f"Duplicate or invalid case role in group: {group}")
        groups[group].add(role)
    if any(roles not in ({"regression", "fix"}, {"control"}) for roles in groups.values()):
        raise ValueError("Groups need a regression/fix pair or one independent control")

    OUTPUT.mkdir(parents=True, exist_ok=True)
    repo_paths: dict[str, Path] = {}
    for key, spec in repositories.items():
        if not re.fullmatch(r"[a-z][a-z0-9-]*", key):
            raise ValueError(f"Invalid repository key: {key}")
        if spec["url"] != f"https://github.com/{spec['name']}.git":
            raise ValueError(f"Unexpected repository URL: {key}")
        repo = OUTPUT / "repos" / key
        repo.parent.mkdir(exist_ok=True)
        if not repo.exists():
            _git("clone", "--quiet", spec["url"], str(repo))
        elif _git("remote", "get-url", "origin", cwd=repo) != spec["url"]:
            raise ValueError(f"Local repository has a different origin: {key}")
        repo_paths[key] = repo

    labels_dir = OUTPUT / "gold"
    labels_dir.mkdir(exist_ok=True)
    rows = []
    for case in cases:
        if not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", case["id"]):
            raise ValueError(f"Invalid case ID: {case['id']}")
        key = case["repository"]
        repo = repo_paths[key]
        if case["source_pr"] != f"https://github.com/{repositories[key]['name']}/pull/{case['pr']}":
            raise ValueError(f"PR identity mismatch: {case['id']}")
        for name in ("base_sha", "head_sha", "merge_base_sha"):
            if not re.fullmatch(r"[0-9a-f]{40}", case[name]):
                raise ValueError(f"Invalid {name}: {case['id']}")
        if subprocess.run(
            ["git", "cat-file", "-e", f"{case['head_sha']}^{{commit}}"],
            cwd=repo,
            capture_output=True,
            check=False,
        ).returncode:
            _git("fetch", "--quiet", "origin", f"pull/{case['pr']}/head", cwd=repo)
        for name in ("base_sha", "head_sha", "merge_base_sha"):
            if _git("cat-file", "-t", case[name], cwd=repo) != "commit":
                raise ValueError(f"Missing pinned commit {name} for {case['id']}")

        snapshot = Snapshot.load(repo, case["base_sha"], case["head_sha"])
        if snapshot.merge_base_sha != case["merge_base_sha"]:
            raise ValueError(f"Merge base changed for {case['id']}")
        changed_lines = {
            item.path: [list(pair) for pair in item.added_ranges]
            for item in snapshot.changed_files
        }
        for finding in case["findings"]:
            ranges = changed_lines.get(finding["path"], [])
            if not any(
                finding["start_line"] <= high and low <= finding["end_line"]
                for low, high in ranges
            ):
                raise ValueError(f"Gold finding is outside changed HEAD lines: {case['id']}")
        if case["expected_reproducer"] not in (
            {"base": "pass", "head": "fail"},
            {"base": "fail", "head": "pass"},
        ):
            raise ValueError(f"Unexpected reproducer expectation: {case['id']}")
        if (case["case_role"] == "regression") != (case["expected_reproducer"]["head"] == "fail"):
            raise ValueError(f"Case role and reproducer expectation disagree: {case['id']}")
        if bool(case["findings"]) != (case["expected_reproducer"]["head"] == "fail"):
            raise ValueError(f"Finding and expectation disagree: {case['id']}")
        if case["case_role"] == "control" and case["expected_reproducer"] != {
            "base": "fail", "head": "pass"
        }:
            raise ValueError(f"Control must demonstrate an intended feature: {case['id']}")
        reproducer = Path(__file__).with_name("reproducers") / case["reproducer"]
        if reproducer.name != case["reproducer"] or not reproducer.is_file():
            raise ValueError(f"Invalid reproducer path: {case['id']}")
        gold = {
            "schema_version": 1,
            "label_revision": source["label_revision"],
            "repo_id": snapshot.repo_id,
            "merge_base_sha": snapshot.merge_base_sha,
            "head_sha": snapshot.head_sha,
            "changed_lines": changed_lines,
            "findings": case["findings"],
        }
        (labels_dir / f"{case['id']}.json").write_text(
            json.dumps(gold, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        rows.append(
            {
                "id": case["id"],
                "root_cause_group": case["root_cause_group"],
                "case_role": case["case_role"],
                "repo": f"repos/{key}",
                "base": snapshot.base_sha,
                "head": snapshot.head_sha,
                "gold": f"gold/{case['id']}.json",
                "strategy": "ast",
                "run_tests": False,
            }
        )
    cases_path = OUTPUT / "cases.json"
    cases_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "label_revision": source["label_revision"],
                "manifest_sha256": hashlib.sha256(SOURCE.read_bytes()).hexdigest(),
                "cases": rows,
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    return cases_path


if __name__ == "__main__":
    print(prepare())
