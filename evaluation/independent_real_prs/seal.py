"""Record or verify the exact public PR pilot inputs before model runs."""

from __future__ import annotations

import argparse
import hashlib
import json
from collections import Counter
from pathlib import Path

from prepare import OUTPUT, PROJECT, SOURCE

ROOT = Path(__file__).resolve().parent
SEAL = ROOT / "FREEZE.json"


def _hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _inputs() -> list[Path]:
    return sorted(
        [PROJECT / "pyproject.toml", PROJECT / "uv.lock", *ROOT.glob("*.py"),
         *ROOT.glob("reproducers/*.py"), *PROJECT.glob("src/pr_review_harness/*.py")]
    )


def build_seal() -> dict:
    manifest = json.loads(SOURCE.read_text(encoding="utf-8"))
    reproduction = json.loads((OUTPUT / "reproduction.json").read_text(encoding="utf-8"))
    cases = manifest["cases"]
    runs = reproduction["runs"]
    if reproduction["manifest_sha256"] != _hash(SOURCE):
        raise ValueError("Reproduction was run against another manifest")
    if len(runs) != 2 * len(cases) or not all(row["as_expected"] for row in runs):
        raise ValueError("Every base/head reproduction must match its expectation")
    actual = {(row["case_id"], row["version"]): row for row in runs}
    if len(actual) != len(runs):
        raise ValueError("Duplicate reproduction result")
    for case in cases:
        script = ROOT / "reproducers" / case["reproducer"]
        for version in ("base", "head"):
            row = actual.get((case["id"], version))
            if row is None or row["reproducer_sha256"] != _hash(script):
                raise ValueError(f"Missing or stale reproducer result: {case['id']} {version}")
            if row["expected"] != case["expected_reproducer"][version]:
                raise ValueError(f"Changed expected outcome: {case['id']} {version}")
    roles = Counter(case["case_role"] for case in cases)
    files = {str(path.relative_to(PROJECT)): _hash(path) for path in _inputs()}
    return {
        "schema_version": 1,
        "purpose": "public SHA-pinned pilot; labels are not private and require human review",
        "case_count": len(cases),
        "root_cause_groups": len({case["root_cause_group"] for case in cases}),
        "roles": dict(sorted(roles.items())),
        "verified_reproductions": len(runs),
        "manifest_sha256": _hash(SOURCE),
        "input_sha256": files,
        "pilot_config": {
            "model": "deepseek-flash",
            "base_url": "https://api.deepseek.com",
            "thinking_mode": "disabled",
            "temperature": 0,
            "variants": ["baseline", "working"],
            "model_calls_per_review": 12,
            "tool_calls_per_review": 24,
            "read_chars_per_review": 40000,
            "run_tests": False,
            "repeats": 1,
        },
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--verify", action="store_true", help="Fail if sealed inputs changed")
    args = parser.parse_args()
    current = build_seal()
    if args.verify:
        saved = json.loads(SEAL.read_text(encoding="utf-8"))
        if saved != current:
            raise SystemExit("Frozen inputs changed; do not overwrite the original pilot")
        print(f"Verified {SEAL}")
        return
    if SEAL.exists():
        raise SystemExit("Freeze already exists; use --verify")
    SEAL.write_text(json.dumps(current, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"Sealed {SEAL}")


if __name__ == "__main__":
    main()
