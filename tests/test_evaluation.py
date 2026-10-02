import json

import pytest

from pr_review_harness.evaluation import evaluate_report, load_gold


def sample_report():
    return {
        "schema_version": 1,
        "mode": "live",
        "repo_id": "repo-1",
        "head_sha": "head-1",
        "merge_base_sha": "base-1",
        "findings": [
            {
                "path": "pricing.py",
                "line": 5,
                "title": "Discount is rounded to zero",
                "evidence_ids": ["check-001"],
            }
        ],
        "evidence": [
            {
                "id": "check-001",
                "base": {"sha": "base-1"},
                "head": {"sha": "head-1"},
            }
        ],
    }


def sample_gold():
    return {
        "schema_version": 1,
        "repo_id": "repo-1",
        "head_sha": "head-1",
        "merge_base_sha": "base-1",
        "changed_lines": {"pricing.py": [[5, 5], [12, 14]]},
        "findings": [
            {
                "id": "discount-floor-division",
                "path": "pricing.py",
                "start_line": 4,
                "end_line": 6,
                "description": "Discounts under 100% are truncated to zero.",
            }
        ],
    }


def test_matches_changed_head_line_inside_allowed_gold_span():
    result = evaluate_report(sample_report(), sample_gold())
    assert result["counts"] == {
        "tp": 1,
        "fp": 0,
        "fn": 0,
        "duplicate": 0,
        "raw_findings": 1,
        "scored_findings": 1,
    }
    assert result["precision"] == result["recall"] == 1.0
    assert result["matches"] == [{"report_index": 0, "label_id": "discount-floor-division"}]
    assert result["metric_scope"] == "changed_head_location_proxy"
    assert result["requires_human_review"] is True


def test_duplicate_claim_is_separate_and_missing_label_is_reported():
    report, gold = sample_report(), sample_gold()
    report["findings"].extend(
        [
            {**report["findings"][0], "title": "Same root cause with new wording"},
            {"path": "pricing.py", "line": 12, "title": "Unlabelled issue", "evidence_ids": []},
            {"path": "pricing.py", "line": 12, "title": "Unlabelled issue!", "evidence_ids": []},
        ]
    )
    gold["findings"].append(
        {"id": "boundary", "path": "pricing.py", "start_line": 14, "end_line": 14}
    )
    result = evaluate_report(report, gold)
    assert result["counts"] == {
        "tp": 1,
        "fp": 1,
        "fn": 1,
        "duplicate": 2,
        "raw_findings": 4,
        "scored_findings": 2,
    }
    assert result["precision"] == result["recall"] == 0.5
    assert result["missed_labels"][0]["id"] == "boundary"
    assert {row["report_index"] for row in result["duplicates"]} == {1, 3}


def test_invalid_changed_line_and_fabricated_evidence_cannot_match_label():
    report = sample_report()
    report["findings"].extend(
        [
            {"path": "pricing.py", "line": 4, "title": "Old line", "evidence_ids": []},
            {"path": "pricing.py", "line": 5, "title": "Made up test", "evidence_ids": ["fake"]},
        ]
    )
    report["findings"][0]["evidence_ids"] = ["fake"]
    result = evaluate_report(report, sample_gold())
    assert result["counts"]["tp"] == 0
    assert result["counts"]["fp"] == 3
    assert result["counts"]["fn"] == 1
    assert result["precision"] == result["recall"] == 0
    assert {row["code"] for row in result["invalid_findings"]} == {
        "unknown_evidence_id",
        "unchanged_head_line",
    }


def test_stale_evidence_and_duplicate_evidence_id_are_diagnosed():
    report = sample_report()
    report["evidence"][0]["head"]["sha"] = "old-head"
    result = evaluate_report(report, sample_gold())
    assert result["counts"]["fp"] == 1
    assert result["diagnostics"][0]["code"] == "stale_evidence"
    assert result["invalid_findings"][0]["code"] == "invalid_evidence_id"

    report["evidence"].append(
        {"id": "check-001", "base": {"sha": "base-1"}, "head": {"sha": "head-1"}}
    )
    result = evaluate_report(report, sample_gold())
    assert any(row["code"] == "duplicate_evidence_id" for row in result["diagnostics"])


def test_zero_denominator_cases_and_scripted_mode():
    report, gold = sample_report(), sample_gold()
    report["findings"] = []
    assert evaluate_report(report, gold)["precision"] is None
    assert evaluate_report(report, gold)["recall"] == 0
    gold["findings"] = []
    report["mode"] = "scripted-demo"
    result = evaluate_report(report, gold)
    assert result["precision"] is result["recall"] is None
    assert result["eligible_for_model_quality"] is False
    report["findings"] = [
        {"path": "pricing.py", "line": 5, "title": "False alarm", "evidence_ids": []}
    ]
    result = evaluate_report(report, gold)
    assert result["precision"] == 0
    assert result["recall"] is None


def test_refuses_stale_or_ambiguous_labels():
    report, gold = sample_report(), sample_gold()
    gold["head_sha"] = "another-head"
    with pytest.raises(ValueError, match="head_sha differ"):
        evaluate_report(report, gold)
    gold["head_sha"] = "head-1"
    gold["findings"].append(
        {"id": "same-line", "path": "pricing.py", "start_line": 5, "end_line": 5}
    )
    with pytest.raises(ValueError, match="overlap"):
        evaluate_report(report, gold)


def test_refuses_gold_changed_lines_that_disagree_with_saved_snapshot():
    report, gold = sample_report(), sample_gold()
    report["changed_lines"] = {"pricing.py": [[5, 5], [12, 14]]}
    gold["changed_lines"] = {"pricing.py": [[4, 5], [12, 14]]}
    with pytest.raises(ValueError, match="changed_lines differ"):
        evaluate_report(report, gold)


def test_load_gold_from_json(tmp_path):
    path = tmp_path / "gold.json"
    path.write_text(json.dumps(sample_gold()), encoding="utf-8")
    assert load_gold(path)["findings"][0]["id"] == "discount-floor-division"
    path.write_text("[]", encoding="utf-8")
    with pytest.raises(ValueError, match="JSON object"):
        load_gold(path)
