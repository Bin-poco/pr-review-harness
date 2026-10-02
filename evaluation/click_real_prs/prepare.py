"""Create local, SHA-pinned Click PR cases and evaluation-only labels."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

from pr_review_harness.snapshot import Snapshot

PROJECT = Path(__file__).resolve().parents[2]
SOURCE = Path(__file__).with_name("manifest.json")
OUTPUT = PROJECT / ".pr-harness" / "evaluation" / "click-real-prs"


def _git(*args: str, cwd: Path | None = None) -> str:
    result = subprocess.run(
        ["git", *args], cwd=cwd, capture_output=True, text=True, check=True, timeout=180
    )
    return result.stdout.strip()


def prepare() -> Path:
    source = json.loads(SOURCE.read_text(encoding="utf-8"))
    if source.get("schema_version") != 1 or not source.get("cases"):
        raise ValueError("Expected a nonempty version 1 PR manifest")
    repository = source["repository"]
    if repository["name"] != "pallets/click":
        raise ValueError("This curated set is pinned to pallets/click")
    OUTPUT.mkdir(parents=True, exist_ok=True)
    repo = OUTPUT / "repo"
    if not repo.exists():
        _git("clone", "--quiet", repository["url"], str(repo))
    elif _git("remote", "get-url", "origin", cwd=repo) != repository["url"]:
        raise ValueError("Local evaluation repo has a different origin URL")

    rows = []
    labels_dir = OUTPUT / "gold"
    labels_dir.mkdir(exist_ok=True)
    for case in source["cases"]:
        if not isinstance(case["id"], str) or not case["id"].replace("-", "").isalnum():
            raise ValueError("Case ID must be an alphanumeric slug")
        for sha in (case["base_sha"], case["head_sha"]):
            if _git("cat-file", "-t", sha, cwd=repo) != "commit":
                raise ValueError(f"Missing pinned commit {sha}")
        snapshot = Snapshot.load(repo, case["base_sha"], case["head_sha"])
        if snapshot.merge_base_sha != case["merge_base_sha"]:
            raise ValueError(f"Merge base changed for {case['id']}")
        changed_lines = {
            changed.path: [list(pair) for pair in changed.added_ranges]
            for changed in snapshot.changed_files
        }
        for finding in case["findings"]:
            ranges = changed_lines.get(finding["path"], [])
            if not any(
                finding["start_line"] <= high and low <= finding["end_line"]
                for low, high in ranges
            ):
                raise ValueError(f"Gold finding is outside changed lines: {case['id']}")
        gold = {
            "schema_version": 1,
            "label_revision": source.get("label_revision", 1),
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
                "repo": "repo",
                "base": snapshot.base_sha,
                "head": snapshot.head_sha,
                "gold": f"gold/{case['id']}.json",
                "strategy": "ast",
                "run_tests": False,
            }
        )
    cases_file = OUTPUT / "cases.json"
    cases_file.write_text(
        json.dumps(
            {"schema_version": 1, "label_revision": source.get("label_revision", 1), "cases": rows},
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    (OUTPUT / "cases-smoke.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "label_revision": source.get("label_revision", 1),
                "cases": rows[:1],
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    return cases_file


if __name__ == "__main__":
    print(prepare())
