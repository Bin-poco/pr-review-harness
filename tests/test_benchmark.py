"""Ablation artifacts are bounded runs, not reported model-quality improvements."""

import json

from test_runtime import make_snapshot

from pr_review_harness.benchmark import run_benchmark
from pr_review_harness.budget import BudgetPolicy
from pr_review_harness.memory import MemoryStore
from pr_review_harness.runtime import DemoChatModel


def test_offline_benchmark_freezes_inputs_and_produces_human_review_template(tmp_path):
    snapshot = make_snapshot(tmp_path)
    memory = MemoryStore(tmp_path / "memory.db")
    memory.add(snapshot.repo_id, "Historical guidance", "previous PR")
    gold = {
        "schema_version": 1,
        "repo_id": snapshot.repo_id,
        "head_sha": snapshot.head_sha,
        "merge_base_sha": snapshot.merge_base_sha,
        "changed_lines": {"pricing.py": [[5, 5]]},
        "findings": [
            {
                "id": "discount",
                "path": "pricing.py",
                "start_line": 5,
                "end_line": 5,
                "description": "Incorrect partial discount",
            }
        ],
    }
    (tmp_path / "gold.json").write_text(json.dumps(gold))
    cases = {
        "schema_version": 1,
        "cases": [
            {
                "id": "discount",
                "repo": str(snapshot.repo),
                "base": snapshot.base_sha,
                "head": snapshot.head_sha,
                "gold": "gold.json",
                "run_tests": True,
                "memory_db": "memory.db",
                "memory_is_prior_feedback": True,
            }
        ],
    }
    cases_path = tmp_path / "cases.json"
    cases_path.write_text(json.dumps(cases))
    target = run_benchmark(
        cases_path, DemoChatModel(), BudgetPolicy(), tmp_path / "results", scripted=True
    )
    result = json.loads(target.read_text())
    assert len(result["runs"]) == 4
    assert result["eligible_for_model_quality"] is False
    assert all(row["status"] == "completed" for row in result["runs"])
    reports = {row["variant"]: json.loads(open(row["report"]).read()) for row in result["runs"]}
    assert reports["baseline"]["context"]["memory_chars"] == 0
    assert reports["baseline"]["context"]["working_state"]["refreshes"] == 0
    assert reports["working"]["context"]["working_state"]["refreshes"] == 3
    assert (
        reports["both"]["context"]["memory_snapshot"]
        == reports["memory"]["context"]["memory_snapshot"]
    )
    human = json.loads((target.parent / "human-review.json").read_text())
    assert len(human["judgments"]) == 4
    assert all(j["root_cause_match"] is None for j in human["judgments"])
    assert all(row["location_counts"]["tp"] == 1 for row in result["runs"])
