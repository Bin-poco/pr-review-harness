"""Rerun only failed submissions from a saved batch, never rewrite its results.

This measures completion on known failure cases; it is not held-out quality evidence.
Load credentials in the process environment, for example with uv --env-file .env.
"""

import argparse
import json
from dataclasses import asdict
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

from pr_review_harness.budget import BudgetPolicy
from pr_review_harness.cli import _live_model
from pr_review_harness.persistence import atomic_json, digest
from pr_review_harness.report import write_report
from pr_review_harness.runtime import ReviewFailure, review
from pr_review_harness.snapshot import Snapshot


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--benchmark", type=Path, required=True)
    parser.add_argument("--out", type=Path, default=Path("outputs/submission-replay"))
    args = parser.parse_args()
    original = json.loads(args.benchmark.read_text())
    target = args.out.resolve() / uuid4().hex
    result = {
        "purpose": "completion diagnostic on previously failed cases; no quality claim",
        "original_benchmark": str(args.benchmark.resolve()),
        "original_sha256": digest(original),
        "runs": [],
    }
    for row in original["runs"]:
        if row["status"] != "failed":
            continue
        failure = row["failure"]
        manifest_path = Path(row["report"]).parent / "runs" / failure["run_id"] / "manifest.json"
        manifest = json.loads(manifest_path.read_text())
        policy = BudgetPolicy(**manifest["budget"])
        model = _live_model(
            SimpleNamespace(
                model=manifest["model"]["model_name"],
                base_url=manifest["model"].get("openai_api_base"),
                api_key_env="HARNESS_API_KEY",
                thinking_mode=manifest["model"]
                .get("extra_body", {})
                .get("thinking", {})
                .get("type"),
                output_tokens=policy.output_tokens,
            )
        )
        snapshot = Snapshot.load(Path(manifest["repo"]), manifest["base_sha"], manifest["head_sha"])
        output = target / f"case-{len(result['runs']):02d}"
        record = {
            "case_id": row["case_id"],
            "variant": row["variant"],
            "old_run_id": failure["run_id"],
            "policy": asdict(policy),
        }
        print(f"Reviewing {row['case_id']} ({row['variant']})", flush=True)
        try:
            report = review(
                snapshot,
                model,
                budget=policy,
                context_strategy=manifest["strategy"],
                run_tests=manifest["run_tests"],
                runs_dir=output / "runs",
            )
        except ReviewFailure as exc:
            atomic_json(output / "failed.json", exc.partial)
            record.update(
                status="failed",
                reason=str(exc),
                submission=exc.partial["submission"],
                budget_usage=exc.partial["budget_usage"],
            )
        else:
            write_report(report, output)
            record.update(
                status="completed",
                run_id=report["run_id"],
                findings=len(report["findings"]),
                submission=report["submission"],
                budget_usage=report["budget_usage"],
            )
        result["runs"].append(record)
        atomic_json(target / "replay.json", result)
        print(
            f"  {record['status']}; corrections={record['submission']['repair_requests']}",
            flush=True,
        )
    print(f"Replay: {target / 'replay.json'}")


if __name__ == "__main__":
    main()
