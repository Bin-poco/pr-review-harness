from pydantic import Field

from pr_review_harness.checks import CheckRunner
from pr_review_harness.demo import create_demo
from pr_review_harness.models import Finding
from pr_review_harness.runtime import DemoChatModel, review, validate_findings
from pr_review_harness.snapshot import Snapshot


class RecordingDemoModel(DemoChatModel):
    system_messages: list[str] = Field(default_factory=list)

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        self.system_messages.extend(str(m.content) for m in messages if m.type == "system")
        return super()._generate(messages, stop, run_manager, **kwargs)


def make_snapshot(tmp_path):
    repo = tmp_path / "source"
    base, head = create_demo(repo)
    return Snapshot.load(repo, base, head)


def test_real_harness_loop_runs_versioned_checks_and_loads_memory(tmp_path):
    snapshot = make_snapshot(tmp_path)
    model = RecordingDemoModel()
    report = review(
        snapshot,
        model,
        run_tests=True,
        mode="scripted-demo",
        memory="KNOWN_FEEDBACK_123",
        model_calls=3,
    )
    assert report["mode"] == "scripted-demo"
    assert report["findings"][0]["line"] == 5
    assert report["evidence"][0]["base"]["status"] == "passed"
    assert report["evidence"][0]["head"]["status"] == "failed"
    assert report["evidence"][0]["same_check"] is True
    assert report["evidence"][0]["base"]["sha"] == snapshot.merge_base_sha
    assert report["evidence"][0]["head"]["sha"] == snapshot.head_sha
    assert "KNOWN_FEEDBACK_123" in "\n".join(model.system_messages)
    assert "python-boundary-regressions" in "\n".join(model.system_messages)
    assert [event["tool"] for event in report["trace"]] == [
        "read_code",
        "run_check",
        "submit_review",
    ]
    assert report["usage"] == {"reported": False}


def test_unittest_execution_requires_explicit_flag(tmp_path):
    import pytest

    snapshot = make_snapshot(tmp_path)
    with pytest.raises(ValueError, match="requires --run-tests"):
        CheckRunner(snapshot).run("unittest", "tests/test_pricing.py")
    assert CheckRunner(snapshot).run("syntax", "pricing.py").head.status == "passed"


def test_finding_rejects_unchanged_locations_and_fabricated_evidence(tmp_path):
    snapshot = make_snapshot(tmp_path)
    common = {
        "path": "pricing.py",
        "line": 5,
        "severity": "P2",
        "title": "discount error",
        "explanation": "floor division loses fractions",
        "trigger": "20 percent discount",
    }
    findings = [Finding(**common, evidence_ids=["invented"])]
    accepted, errors = validate_findings(snapshot, findings, [])
    assert not accepted and "evidence" in errors[0]
    common["line"] = 1
    accepted, errors = validate_findings(snapshot, [Finding(**common)], [])
    assert not accepted and "changed head line" in errors[0]


def test_same_file_title_is_deduplicated(tmp_path):
    snapshot = make_snapshot(tmp_path)
    finding = Finding(
        path="pricing.py",
        line=5,
        severity="P2",
        title="Discount error!",
        explanation="floor division",
        trigger="20 percent discount",
    )
    duplicate = finding.model_copy(update={"title": "discount ERROR"})
    accepted, errors = validate_findings(snapshot, [finding, duplicate], [])
    assert len(accepted) == 1 and not errors


def test_budget_failure_keeps_completed_evidence(tmp_path):
    import pytest

    from pr_review_harness.runtime import ReviewFailure

    snapshot = make_snapshot(tmp_path)
    with pytest.raises(ReviewFailure, match="ModelCallLimitExceededError") as caught:
        review(snapshot, DemoChatModel(), run_tests=True, model_calls=2)
    assert caught.value.partial["status"] == "failed"
    assert caught.value.partial["evidence"][0]["head"]["status"] == "failed"
    assert "findings" not in caught.value.partial


def test_changed_tests_are_not_reported_as_same_check(tmp_path):
    import subprocess

    snapshot = make_snapshot(tmp_path)
    test_path = snapshot.repo / "tests/test_pricing.py"
    test_path.write_text(
        test_path.read_text().replace("apply_discount(100, 20), 80", "apply_discount(100, 20), 100")
    )
    subprocess.run(["git", "-C", str(snapshot.repo), "add", "."], check=True)
    subprocess.run(
        [
            "git",
            "-C",
            str(snapshot.repo),
            "-c",
            "core.hooksPath=/dev/null",
            "commit",
            "-qm",
            "changed test expectation",
        ],
        check=True,
    )
    current = Snapshot.load(snapshot.repo, snapshot.merge_base_sha, "HEAD")
    result = CheckRunner(current, run_tests=True).run("unittest", "tests/test_pricing.py")
    assert result.base.status == result.head.status == "passed"
    assert result.same_check is False
