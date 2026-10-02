"""Reproduce public regressions and fixes on both immutable PR snapshots."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

from prepare import OUTPUT, SOURCE, prepare

from pr_review_harness.snapshot import Snapshot


def _runtime_python(key: str, spec: dict) -> Path:
    venv = OUTPUT / "venvs" / key
    python = venv / "bin" / "python"
    marker = venv / "eval-environment.json"
    identity = {"python": spec["python"], "dependencies": spec["dependencies"]}
    if marker.exists() and json.loads(marker.read_text(encoding="utf-8")) == identity:
        if python.is_file():
            return python
    subprocess.run(
        ["uv", "venv", "--clear", "--python", spec["python"], str(venv)], check=True
    )
    subprocess.run(
        ["uv", "pip", "install", "--python", str(python), *spec["dependencies"]],
        check=True,
    )
    marker.write_text(json.dumps(identity, indent=2) + "\n", encoding="utf-8")
    return python


def reproduce() -> Path:
    prepare()
    manifest = json.loads(SOURCE.read_text(encoding="utf-8"))
    pythons = {
        key: _runtime_python(key, spec)
        for key, spec in manifest["repositories"].items()
    }
    results = []
    with tempfile.TemporaryDirectory(prefix="independent-pr-repro-", dir=OUTPUT) as temporary:
        scratch = Path(temporary)
        for case in manifest["cases"]:
            key = case["repository"]
            spec = manifest["repositories"][key]
            snapshot = Snapshot.load(
                OUTPUT / "repos" / key, case["base_sha"], case["head_sha"]
            )
            script = Path(__file__).with_name("reproducers") / case["reproducer"]
            script_hash = hashlib.sha256(script.read_bytes()).hexdigest()
            for version in ("base", "head"):
                tree = scratch / case["id"] / version
                snapshot.export(version, tree)
                env = os.environ.copy()
                env["PYTHONPATH"] = str(tree / spec["source_root"])
                env["PYTHONDONTWRITEBYTECODE"] = "1"
                env["PYTHONNOUSERSITE"] = "1"
                # Verify that the reproducer imports the exported Git snapshot,
                # rather than an installed dependency wheel in the evaluation venv.
                source_check = subprocess.run(
                    [
                        str(pythons[key]),
                        "-c",
                        "import importlib.util, pathlib, sys; "
                        "found = importlib.util.find_spec(sys.argv[1]); "
                        "origin = pathlib.Path(found.origin).resolve() "
                        "if found and found.origin else None; "
                        "root = pathlib.Path(sys.argv[2]).resolve(); "
                        "assert origin and origin.is_relative_to(root), (origin, root)",
                        spec["source_module"],
                        str(tree / spec["source_root"]),
                    ],
                    cwd=tree,
                    env=env,
                    capture_output=True,
                    text=True,
                    timeout=30,
                )
                if source_check.returncode:
                    raise RuntimeError(
                        f"Reproducer would not import the pinned {key} source at "
                        f"{case['id']} {version}: {source_check.stderr[-500:]}"
                    )
                process = subprocess.run(
                    [str(pythons[key]), str(script)],
                    cwd=tree,
                    env=env,
                    capture_output=True,
                    text=True,
                    timeout=30,
                )
                outcome = "pass" if process.returncode == 0 else "fail"
                expected = case["expected_reproducer"][version]
                results.append(
                    {
                        "case_id": case["id"],
                        "version": version,
                        "sha": snapshot.merge_base_sha if version == "base" else snapshot.head_sha,
                        "reproducer_sha256": script_hash,
                        "outcome": outcome,
                        "expected": expected,
                        "as_expected": outcome == expected,
                        "error_tail": process.stderr[-500:] if process.returncode else "",
                    }
                )
    target = OUTPUT / "reproduction.json"
    target.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "manifest_sha256": hashlib.sha256(SOURCE.read_bytes()).hexdigest(),
                "runner_python": sys.version,
                "runs": results,
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    if not all(row["as_expected"] for row in results):
        raise RuntimeError(f"Reproducer results did not match pinned expectations: {target}")
    return target


if __name__ == "__main__":
    print(reproduce())
