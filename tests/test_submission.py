"""Exercise final-output failures in the actual graph, including resumed correction."""

import json

import pytest
from langchain_core.messages import AIMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from test_runtime import make_snapshot

from pr_review_harness.budget import BudgetPolicy
from pr_review_harness.runtime import DemoChatModel, ReviewFailure, review


class BrokenFinalModel(DemoChatModel):
    broken: str = "length"
    always_broken: bool = False
    interrupt_repair: bool = False

    def bind_tools(self, tools, *, tool_choice=None, **kwargs):
        return self.bind(tool_choice=tool_choice)

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        previous = [m for m in messages if isinstance(m, AIMessage)]
        if previous and self.interrupt_repair:
            raise RuntimeError("interrupted correction")
        if not previous or self.always_broken:
            if self.broken == "invalid":
                result = AIMessage(
                    content="",
                    invalid_tool_calls=[
                        {
                            "name": "submit_review",
                            "args": '{"findings":[',
                            "id": "malformed",
                            "error": "Invalid JSON",
                        }
                    ],
                    response_metadata={"finish_reason": "tool_calls"},
                )
            elif self.broken == "schema":
                result = AIMessage(
                    content="",
                    tool_calls=[
                        {
                            "name": "submit_review",
                            "args": {"findings": "not-a-list"},
                            "id": "bad-schema",
                        }
                    ],
                )
            elif self.broken == "location":
                result = AIMessage(
                    content="",
                    tool_calls=[
                        {
                            "name": "submit_review",
                            "args": {
                                "findings": [
                                    {
                                        "path": "pricing.py",
                                        "line": 1,
                                        "severity": "P2",
                                        "title": "wrong",
                                        "trigger": "input",
                                        "explanation": "Wrong location",
                                    }
                                ]
                            },
                            "id": "bad-location",
                        }
                    ],
                )
            else:
                result = AIMessage(
                    content="unfinished review",
                    response_metadata={
                        "finish_reason": "length" if self.broken == "length" else "stop",
                    },
                )
        else:
            assert kwargs.get("tool_choice") == "submit_review"
            result = AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "submit_review",
                        "args": {"findings": []},
                        "id": "corrected",
                    }
                ],
            )
        return ChatResult(generations=[ChatGeneration(message=result)])


@pytest.mark.parametrize(
    "broken,reason",
    [
        ("length", "output_truncated"),
        ("invalid", "invalid_tool_arguments"),
        ("plain", "missing_submission"),
        ("schema", "submission_schema_error"),
        ("location", "submission_validation_error"),
    ],
)
def test_repair_uses_existing_budget_and_preserves_original_output(tmp_path, broken, reason):
    report = review(
        make_snapshot(tmp_path),
        BrokenFinalModel(broken=broken),
        runs_dir=tmp_path / "runs",
        run_id=broken,
    )
    assert report["findings"] == []
    assert report["submission"]["outcome"] == "submitted"
    assert report["submission"]["repair_requests"] == 1
    assert report["submission"]["last_failure"] == reason
    assert report["budget_usage"]["model_attempts"] == 2
    assert len(report["messages"]) >= 3
    if broken == "length":
        assert report["submission"]["observations"][0]["finish_reason"] == "length"
    if broken == "invalid":
        assert report["submission"]["observations"][0]["invalid_tool_calls"]


def test_exhausted_repairs_remain_failure_and_save_full_diagnostics(tmp_path):
    with pytest.raises(ReviewFailure) as caught:
        review(
            make_snapshot(tmp_path),
            BrokenFinalModel(always_broken=True),
            runs_dir=tmp_path / "runs",
            run_id="exhausted",
        )
    partial = caught.value.partial
    assert "findings" not in partial
    assert partial["submission"]["repair_requests"] == 2
    assert partial["submission"]["stop_reason"] == "repair_limit_reached"
    assert partial["budget_usage"]["model_attempts"] == 3
    assert json.loads((tmp_path / "runs/exhausted/failed.json").read_text()) == partial


@pytest.mark.parametrize(
    "policy,stop",
    [
        (BudgetPolicy(model_calls=1), "model_budget_exhausted"),
        (BudgetPolicy(submission_repairs=0), "repair_limit_reached"),
    ],
)
def test_correction_never_extends_call_limits(tmp_path, policy, stop):
    with pytest.raises(ReviewFailure) as caught:
        review(make_snapshot(tmp_path), BrokenFinalModel(), budget=policy)
    assert caught.value.partial["submission"]["stop_reason"] == stop
    assert caught.value.partial["budget_usage"]["model_attempts"] == 1


def test_resume_keeps_scheduled_correction_and_cumulative_attempts(tmp_path):
    snapshot = make_snapshot(tmp_path)
    options = {"runs_dir": tmp_path / "runs", "run_id": "resume-repair"}
    with pytest.raises(ReviewFailure):
        review(snapshot, BrokenFinalModel(interrupt_repair=True), **options)
    report = review(snapshot, BrokenFinalModel(), resume=True, **options)
    assert report["submission"]["repair_requests"] == 1
    assert report["budget_usage"]["model_attempts"] == 3
    again = review(snapshot, BrokenFinalModel(), resume=True, **options)
    assert again["submission"] == report["submission"]
    assert again["budget_usage"] == report["budget_usage"]


def test_repeated_schema_failure_with_reused_call_id_still_stops_at_repair_limit(tmp_path):
    with pytest.raises(ReviewFailure) as caught:
        review(make_snapshot(tmp_path), BrokenFinalModel(broken="schema", always_broken=True))
    assert caught.value.partial["submission"]["stop_reason"] == "repair_limit_reached"
    assert caught.value.partial["budget_usage"]["model_attempts"] == 3


def test_correction_is_not_scheduled_when_tool_budget_is_exhausted(tmp_path):
    with pytest.raises(ReviewFailure) as caught:
        review(
            make_snapshot(tmp_path),
            BrokenFinalModel(broken="location"),
            budget=BudgetPolicy(tool_calls=1),
        )
    assert caught.value.partial["submission"]["stop_reason"] == "tool_budget_exhausted"
    assert caught.value.partial["budget_usage"]["model_attempts"] == 1
