"""Generate a business-repository workflow using a pinned, trusted harness commit.

This writes a local file only. It does not push, enable Actions, or add secrets.
"""

import argparse
import json
import re
from pathlib import Path

TEMPLATE = Path(__file__).resolve().parents[1] / ".github/workflows/review-pr.yml"


def render(repository, revision):
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository) or any(
        part in {".", ".."} for part in repository.split("/")
    ):
        raise ValueError("Use a trusted public harness repository: OWNER/REPO")
    if not re.fullmatch(r"[0-9a-f]{40}", revision):
        raise ValueError("Pin the harness to a full 40-character commit SHA")
    source = TEMPLATE.read_text(encoding="utf-8")
    original = "          ref: ${{ github.sha }}"
    if source.count(original) != 1:
        raise ValueError("Workflow checkout template changed; inspect before deploying")
    return source.replace(
        original,
        f"          repository: {json.dumps(repository)}\n          ref: '{revision}'",
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--harness-repo", required=True)
    parser.add_argument("--harness-sha", required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    content = render(args.harness_repo, args.harness_sha)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    # Avoid replacing an existing business workflow without a reviewed diff.
    with args.out.open("x", encoding="utf-8") as stream:
        stream.write(content)
    print(f"Workflow proposal: {args.out.resolve()}")


if __name__ == "__main__":
    main()
