"""Run the public commands through an offline review, verifier and evaluation."""

import json

from pr_review_harness.cli import _dispatch, parser


def test_deepseek_thinking_mode_is_explicit_and_saved_without_key(monkeypatch):
    from pr_review_harness.cli import _live_model
    from pr_review_harness.persistence import model_identity

    monkeypatch.setenv("HARNESS_API_KEY", "test-only-secret")
    args = parser().parse_args(
        [
            "benchmark",
            "--cases",
            "cases.json",
            "--model",
            "deepseek-flash",
            "--base-url",
            "https://api.deepseek.com",
            "--thinking-mode",
            "disabled",
        ]
    )
    identity = model_identity(_live_model(args))
    assert identity["extra_body"] == {"thinking": {"type": "disabled"}}
    assert identity["model_kwargs"] == {"parallel_tool_calls": False}
    assert "test-only-secret" not in json.dumps(identity)


def test_benchmark_accepts_review_limits():
    from pr_review_harness.cli import _policy

    args = parser().parse_args(
        [
            "benchmark", "--cases", "cases.json",
            "--model-calls", "24", "--tool-calls", "48", "--read-chars", "80000",
        ]
    )
    policy = _policy(args, None)
    assert (policy.model_calls, policy.tool_calls, policy.read_chars) == (24, 48, 80000)


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


def test_memory_lifecycle_commands(tmp_path):
    from pr_review_harness.demo import create_demo
    from pr_review_harness.memory import MemoryStore
    from pr_review_harness.snapshot import Snapshot

    repo = tmp_path / "repo"
    create_demo(repo)
    db = tmp_path / "memory.db"
    common = ["--repo", str(repo), "--memory-db", str(db)]
    assert (
        _dispatch(
            parser().parse_args(
                [
                    "memory",
                    "add",
                    *common,
                    "--text",
                    "Original rule",
                    "--source",
                    "PR 1",
                    "--scope",
                    "pricing.py",
                ]
            )
        )
        == 0
    )
    assert (
        _dispatch(
            parser().parse_args(
                [
                    "memory",
                    "revise",
                    *common,
                    "--id",
                    "1",
                    "--text",
                    "Updated rule",
                    "--source",
                    "PR 2",
                    "--reason",
                    "Clarification",
                ]
            )
        )
        == 0
    )
    store = MemoryStore(db)
    repo_id = Snapshot.load(repo, "HEAD", "HEAD").repo_id
    assert "Original rule" not in store.recall(repo_id, ["pricing.py"])
    assert "Updated rule" in store.recall(repo_id, ["pricing.py"])
    assert (
        _dispatch(
            parser().parse_args(["memory", "revoke", *common, "--id", "2", "--reason", "Retired"])
        )
        == 0
    )
    assert store.recall(repo_id, ["pricing.py"]) == ""


def test_resume_verify_and_linked_feedback_commands(tmp_path):
    from pr_review_harness.memory import MemoryStore

    out = tmp_path / "demo"
    runs = tmp_path / "runs"
    db = tmp_path / "memory.db"
    assert (
        _dispatch(
            parser().parse_args(
                [
                    "demo",
                    "--out",
                    str(out),
                    "--runs-dir",
                    str(runs),
                    "--run-id",
                    "cli",
                    "--memory-db",
                    str(db),
                ]
            )
        )
        == 0
    )
    first = json.loads((out / "review.json").read_text())
    restored = tmp_path / "restored"
    assert (
        _dispatch(
            parser().parse_args(
                ["resume", "--run-id", "cli", "--runs-dir", str(runs), "--out", str(restored)]
            )
        )
        == 0
    )
    second = json.loads((restored / "review.json").read_text())
    assert first["evidence"] == second["evidence"]
    verified = tmp_path / "verified"
    assert (
        _dispatch(
            parser().parse_args(
                ["verify", "--report", str(out / "review.json"), "--out", str(verified)]
            )
        )
        == 0
    )
    assert (
        json.loads((verified / "review.json").read_text())["verification"]["status"] == "completed"
    )
    assert (
        _dispatch(
            parser().parse_args(
                [
                    "memory",
                    "feedback",
                    "--repo",
                    first["repo"],
                    "--report",
                    str(out / "review.json"),
                    "--finding-id",
                    first["findings"][0]["id"],
                    "--text",
                    "Explicit human feedback",
                    "--source",
                    "PR comment",
                    "--rule-key",
                    "pricing.discount",
                    "--memory-db",
                    str(db),
                ]
            )
        )
        == 0
    )
    record = MemoryStore(db).list_records(first["repo_id"])[0]
    assert record["source_run_id"] == "cli"
    assert record["finding_id"] == first["findings"][0]["id"]
    assert record["disposition"] == "dismissed"
