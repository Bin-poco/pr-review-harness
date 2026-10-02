"""Rescore a saved batch after a transparent label revision, without rerunning agents."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

from prepare import OUTPUT, SOURCE, prepare

from pr_review_harness.evaluation import evaluate_report, load_gold


def rescore(benchmark_path: Path) -> Path:
    prepare()
    benchmark_path = benchmark_path.resolve()
    batch = json.loads(benchmark_path.read_text(encoding="utf-8"))
    source = json.loads(SOURCE.read_text(encoding="utf-8"))
    valid_ids = {case["id"] for case in source["cases"]}
    rows = []
    for original in batch["runs"]:
        case_id = original["case_id"]
        if case_id not in valid_ids:
            raise ValueError(f"Unknown Click case in saved batch: {case_id}")
        row = {"case_id": case_id, "variant": original["variant"], "status": original["status"]}
        if original["status"] == "completed":
            report = json.loads(Path(original["report"]).read_text(encoding="utf-8"))
            gold_path = OUTPUT / "gold" / f"{case_id}.json"
            score = evaluate_report(report, load_gold(gold_path))
            row.update(
                counts=score["counts"],
                matches=score["matches"],
                false_positives=score["false_positives"],
                missed_labels=[item["id"] for item in score["missed_labels"]],
                gold_sha256=hashlib.sha256(gold_path.read_bytes()).hexdigest(),
            )
        rows.append(row)
    target = benchmark_path.with_name(f"rescore-label-{source['label_revision']}.json")
    target.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "label_revision": source["label_revision"],
                "source_batch": str(benchmark_path),
                "eligible_for_model_quality": False,
                "note": "Location matching only; human root-cause judgments are separate.",
                "runs": rows,
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    return target


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("benchmark", type=Path)
    print(rescore(parser.parse_args().benchmark))
