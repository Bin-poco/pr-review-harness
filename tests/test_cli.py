"""Run the public commands through an offline review, verifier and evaluation."""

import json

from pr_review_harness.cli import _dispatch, parser


def test_demo_verify_and_evaluate_commands(tmp_path):
    output = tmp_path / "demo"
    demo = parser().parse_args(["demo", "--verify", "--out", str(output)])
    assert _dispatch(demo) == 0
    report_path = output / "review.json"
    report = json.loads(report_path.read_text(encoding="utf-8"))
    assert report["verification"]["status"] == "completed"
    assert report["context"]["available_skills"] == [
        "python-boundary-regressions",
        "python-api-compatibility",
    ]
    assert "独立核验" in (output / "review.md").read_text(encoding="utf-8")

    gold = {
        "schema_version": 1,
        "repo_id": report["repo_id"],
        "head_sha": report["head_sha"],
        "merge_base_sha": report["merge_base_sha"],
        "changed_lines": {"pricing.py": [[5, 5]]},
        "findings": [
            {
                "id": "discount-regression",
                "path": "pricing.py",
                "start_line": 5,
                "end_line": 5,
                "description": "Partial discounts are lost to floor division.",
            }
        ],
    }
    gold_path = tmp_path / "gold.json"
    gold_path.write_text(json.dumps(gold), encoding="utf-8")
    result_dir = tmp_path / "scored"
    command = parser().parse_args(
        [
            "evaluate",
            "--report",
            str(report_path),
            "--gold",
            str(gold_path),
            "--out",
            str(result_dir),
        ]
    )
    assert _dispatch(command) == 0
    result = json.loads((result_dir / "evaluation.json").read_text(encoding="utf-8"))
    assert result["counts"] == {
        "tp": 1,
        "fp": 0,
        "fn": 0,
        "duplicate": 0,
        "raw_findings": 1,
        "scored_findings": 1,
    }
    assert result["is_live_model_run"] is False
    assert result["eligible_for_model_quality"] is False
