"""Repeat fixed-version cases with controlled context/memory ablations.

Gold labels are only read after every agent run has completed. These location
scores are candidates for human root-cause review, never automatic quality claims.
"""

import json
from dataclasses import replace
from pathlib import Path
from uuid import uuid4

from pr_review_harness.evaluation import evaluate_report, load_gold
from pr_review_harness.memory import MemoryStore
from pr_review_harness.persistence import atomic_json, digest, model_identity
from pr_review_harness.report import write_report
from pr_review_harness.runtime import ReviewFailure, review
from pr_review_harness.snapshot import Snapshot

VARIANTS = {
    "baseline": (False, False),
    "working": (True, False),
    "memory": (False, True),
    "both": (True, True),
}


def run_benchmark(cases_path, model, policy, output, *, variants=None, scripted=False):
    cases_path = Path(cases_path).resolve()
    output = Path(output).resolve()
    data = json.loads(cases_path.read_text(encoding="utf-8"))
    cases = data.get("cases")
    if data.get("schema_version") != 1 or not isinstance(cases, list) or not cases:
        raise ValueError("Benchmark requires schema_version 1 and a nonempty cases list")
    variants = variants or list(VARIANTS)
    if len(variants) != len(set(variants)) or any(v not in VARIANTS for v in variants):
        raise ValueError("Choose distinct baseline, working, memory or both variants")
    ids = [case.get("id") for case in cases]
    if any(not isinstance(i, str) or not i for i in ids) or len(ids) != len(set(ids)):
        raise ValueError("Benchmark case IDs must be unique nonempty strings")
    batch_id = uuid4().hex
    rows = []
    completed = []
    for case in cases:

        def resolve(value):
            return (cases_path.parent / value).resolve()

        snapshot = Snapshot.load(resolve(case["repo"]), case["base"], case["head"])
        memory = ""
        if case.get("memory_db"):
            if not case.get("memory_is_prior_feedback"):
                raise ValueError(
                    "Each memory_db case must declare memory_is_prior_feedback=true; "
                    "curate historical feedback separately from evaluation labels"
                )
            memory = MemoryStore(resolve(case["memory_db"])).freeze_snapshot(
                snapshot.repo_id, [f.path for f in snapshot.changed_files], policy.memory_chars
            )
        for variant in variants:
            ledger, use_memory = VARIANTS[variant]
            # Case IDs stay display-only; filesystem names are generated identities.
            target = output / batch_id / f"case-{len(rows):04d}-{variant}"
            row = {
                "case_id": case["id"],
                **{key: case[key] for key in ("root_cause_group", "case_role") if key in case},
                "variant": variant,
                "repo_id": snapshot.repo_id,
                "head_sha": snapshot.head_sha,
                "merge_base_sha": snapshot.merge_base_sha,
                "strategy": case.get("strategy", "ast"),
                "model": model_identity(model),
                "policy": replace(policy, working_state=ledger).manifest(),
                "memory_requested": use_memory,
                "historical_memory_declared": bool(case.get("memory_is_prior_feedback")),
            }
            try:
                report = review(
                    snapshot,
                    model,
                    memory=memory if use_memory else "",
                    budget=replace(policy, working_state=ledger),
                    context_strategy=case.get("strategy", "ast"),
                    run_tests=case.get("run_tests", False),
                    mode="scripted-demo" if scripted else "live",
                    runs_dir=target / "runs",
                )
            except ReviewFailure as exc:
                atomic_json(target / "failed.json", exc.partial)
                row.update(status="failed", failure=exc.partial, report=str(target / "failed.json"))
            else:
                paths = write_report(report, target)
                row.update(
                    status="completed",
                    report=str(paths[0]),
                    elapsed_seconds=report["elapsed_seconds"],
                    usage=report["budget_usage"],
                    context_sha256=report["context"]["sha256"],
                    memory_sha256=report["context"]["memory_snapshot"]["sha256"],
                )
                completed.append((row, report, resolve(case["gold"])))
            rows.append(row)
            # Persist progress even if a later case or provider fails.
            atomic_json(output / batch_id / "progress.json", {"runs": rows})
    judgments = []
    # Labels never enter context assembly or the agent's tool arguments.
    for row, report, gold_path in completed:
        score = evaluate_report(report, load_gold(gold_path))
        atomic_json(Path(row["report"]).parent / "evaluation.json", score)
        row["location_counts"] = score["counts"]
        row["label_sha256"] = digest(load_gold(gold_path))
        judgments.extend(
            {
                "case_id": row["case_id"],
                "variant": row["variant"],
                "run_id": report["run_id"],
                "finding_id": f["id"],
                "path": f["path"],
                "line": f["line"],
                "root_cause_match": None,
                "false_positive": None,
                "human_reason": "",
            }
            for f in report["findings"]
        )
    result = {
        "schema_version": 1,
        "batch_id": batch_id,
        "scripted": scripted,
        "eligible_for_model_quality": False,
        "requires_human_root_cause_review": True,
        "comparison": "fixed SHA/model/tools/input cap; working state and memory varied",
        "runs": rows,
        "case_manifest_sha256": digest(data),
    }
    atomic_json(output / batch_id / "benchmark.json", result)
    atomic_json(output / batch_id / "human-review.json", {"judgments": judgments})
    return output / batch_id / "benchmark.json"
