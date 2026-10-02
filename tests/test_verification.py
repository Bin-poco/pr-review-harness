"""Exercise the verifier as a separate bounded tool loop."""

import json

import pytest

from pr_review_harness.demo import create_demo
from pr_review_harness.runtime import DemoChatModel, review
from pr_review_harness.snapshot import Snapshot
from pr_review_harness.verification import (
    DemoVerifierModel,
    _tools,
    _VerificationSession,
    verify_report,
)


def _review(tmp_path):
    repo = tmp_path / "source"
    base, head = create_demo(repo)
    snapshot = Snapshot.load(repo, base, head)
    return snapshot, review(snapshot, DemoChatModel(), run_tests=True, mode="scripted-demo")


def test_independent_pass_reads_both_versions_and_keeps_original_finding(tmp_path):
    snapshot, first = _review(tmp_path)
    checked = verify_report(snapshot, first, DemoVerifierModel())
    assert checked["findings"] == first["findings"]
    assert checked["verification"]["status"] == "completed"
    assert checked["verification"]["verdicts"][0]["verdict"] == "supported"
    assert [event["arguments"]["version"] for event in checked["verification"]["trace"][:2]] == [
        "head",
        "base",
    ]
    assert checked["verification"]["read_chars"] > 0


def test_verifier_requires_two_source_reads_before_accepting_verdict(tmp_path):
    snapshot, first = _review(tmp_path)
    session = _VerificationSession()
    read_code, submit = _tools(snapshot, first, 1, session)
    decision = {
        "finding_index": 1,
        "verdict": "supported",
        "reason": "The claimed result is visible in the changed calculation.",
    }
    rejected = json.loads(submit.invoke({"decisions": [decision]}))
    assert rejected["accepted"] is False
    assert "both head and base" in rejected["errors"][0]
    read_code.invoke({"path": "pricing.py", "version": "head"})
    read_code.invoke({"path": "pricing.py", "version": "base"})
    accepted = json.loads(submit.invoke({"decisions": [decision]}))
    assert accepted["accepted"] is True


def test_truncated_or_irrelevant_reads_cannot_support_a_claim(tmp_path):
    snapshot, first = _review(tmp_path)
    session = _VerificationSession(read_chars=20000)
    read_code, submit = _tools(snapshot, first, 1, session)
    read_code.invoke({"path": "pricing.py", "version": "head"})
    read_code.invoke({"path": "pricing.py", "version": "base"})
    decision = {
        "finding_index": 1,
        "verdict": "supported",
        "reason": "The changed calculation causes an incorrect discount result.",
    }
    rejected = json.loads(submit.invoke({"decisions": [decision]}))
    assert rejected["accepted"] is False
    assert "readable changed head code" in rejected["errors"][0]
    uncertain = {**decision, "verdict": "uncertain"}
    assert json.loads(submit.invoke({"decisions": [uncertain]}))["accepted"] is True


def test_verifier_rejects_stale_snapshot_and_marks_unverified_findings(tmp_path):
    snapshot, first = _review(tmp_path)
    stale = {**first, "head_sha": "0" * 40}
    with pytest.raises(ValueError, match="does not match"):
        verify_report(snapshot, stale, DemoVerifierModel())
    extra = {**first, "findings": first["findings"] * 2}
    checked = verify_report(snapshot, extra, DemoVerifierModel(), max_findings=1)
    assert checked["verification"]["status"] == "partial"
    assert checked["verification"]["unverified_count"] == 1


def test_no_findings_skips_second_model_call(tmp_path):
    snapshot, first = _review(tmp_path)
    empty = {**first, "findings": []}
    checked = verify_report(snapshot, empty, DemoVerifierModel())
    assert checked["verification"] == {"status": "skipped", "reason": "no findings"}
