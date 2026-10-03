"""Independent-stage source provenance, clipping, compaction and receipt replay."""

import hashlib
import json
import subprocess
import sys

import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from pydantic import Field
from test_context_fragments import empty_pack, source_repo
from test_runtime import make_snapshot
from test_snapshot_context import commit

from pr_review_harness.budget import BudgetPolicy, RequestCounter
from pr_review_harness.context import _Builder, _numbered
from pr_review_harness.context_fragments import FragmentIndex
from pr_review_harness.context_manager import VERIFICATION_LEDGER_HEADER, VerificationContext
from pr_review_harness.persistence import RunStore
from pr_review_harness.review_state import ReceiptStateBackend
from pr_review_harness.runtime import DemoChatModel, review
from pr_review_harness.snapshot import Snapshot, git_lines
from pr_review_harness.verification import (
    DemoVerifierModel,
    _packet,
    _tools,
    _VerificationSession,
    verify_report,
)


def assert_code_digests(snapshot, records):
    for record in records:
        assert record["sha"] == (
            snapshot.head_sha if record["version"] == "head" else snapshot.merge_base_sha
        )
        if record["complete"]:
            start, end = record["line_range"]
            code = "\n".join(
                git_lines(snapshot.read_file(record["path"], record["version"]))[start - 1 : end]
            )
            assert record["text_sha256"] == hashlib.sha256(code.encode()).hexdigest()


def candidate(snapshot, path="pricing.py"):
    return {
        "repo_id": snapshot.repo_id,
        "base_sha": snapshot.base_sha,
        "head_sha": snapshot.head_sha,
        "merge_base_sha": snapshot.merge_base_sha,
        "findings": [
            {
                "path": path,
                "line": 1,
                "severity": "P2",
                "title": "Candidate defect",
                "explanation": "Candidate for plumbing tests",
                "trigger": "A changed result",
                "evidence_ids": [],
                "confidence": "medium",
            }
        ],
    }


def test_verifier_packet_indexes_only_actual_diff_and_marks_clipped_line(tmp_path):
    root, base = source_repo(tmp_path, {"large.py": "value = '" + "a" * 2400 + "'\n"})
    (root / "large.py").write_text("value = '" + "b" * 2400 + "'\n")
    snapshot = Snapshot(root, base, commit(root, "head"))
    report = candidate(snapshot, "large.py")
    report["findings"][0]["explanation"] = "@@ -999 +999 @@\n+invented = 1"
    packet = _packet(snapshot, report, 1)
    item = packet.items[0]
    assert packet.text[item.content_start : item.content_start + len(item.content)] == item.content
    assert item.complete_chars < item.source_chars == 1800
    index = FragmentIndex(snapshot, _VerificationSession(), packet)
    records = index.manifest()["records"]
    assert records and all(r["line_range"] == [1, 1] for r in records)
    assert any(not r["complete"] for r in records)
    assert_code_digests(snapshot, records)
    shown = index.visible([HumanMessage(packet.text)], packet.text)
    assert shown and any(v["partial_ranges"] for v in shown)
    assert all(not v["complete_ranges"] for v in shown if v["partial_ranges"])


@pytest.mark.parametrize("newline", ["\n", "\r\n"])
def test_verifier_read_receipt_tracks_complete_prefix_and_partial_tail(tmp_path, newline):
    code = newline.join(["first = 1", "second = 'A\u2028B'", "third = 3", ""])
    root, sha = source_repo(tmp_path, {"lines.py": code})
    snapshot = Snapshot(root, sha, sha)
    session = _VerificationSession()
    tools = _tools(
        snapshot, candidate(snapshot, "lines.py"), 1, session, BudgetPolicy(verify_read_chars=19)
    )
    raw = tools[0].invoke({"path": "lines.py", "version": "head"})
    output = json.loads(raw)
    assert output["truncated"] and output["returned_range"] == [1, 1]
    index = FragmentIndex(snapshot, session, empty_pack())
    records = index.manifest()["records"]
    assert len(records) == 2
    assert {(tuple(r["line_range"]), r["complete"]) for r in records} == {
        ((1, 1), True),
        ((2, 2), False),
    }
    assert_code_digests(snapshot, records)
    visible = index.visible([ToolMessage(raw, name="read_code", tool_call_id="read")], "")
    assert {tuple(v["complete_ranges"][0]) for v in visible if v["complete_ranges"]} == {(1, 1)}
    assert {tuple(v["partial_ranges"][0]) for v in visible if v["partial_ranges"]} == {(2, 2)}
    assert not session.readable_ranges  # Verdict eligibility remains conservative.


def test_budget_omission_marker_cannot_extend_literal_source_coverage(tmp_path):
    snapshot = make_snapshot(tmp_path)
    builder = _Builder(10000, "")
    builder.add("pricing.py", "test", _numbered(snapshot.read_file("pricing.py")), 10000)
    packet = builder.finish()
    counter = RequestCounter(DemoChatModel(), BudgetPolicy())
    marker = "\n[OMITTED by unified budget; use fixed-version tools for original.]"
    # Clip before the first source character, despite a marker longer than a code line.
    prefix = packet.items[0].content_start
    displayed, source_chars = counter.fit_prefix(
        packet.text, counter.text(packet.text[:prefix] + marker)
    )
    assert source_chars == prefix and len(displayed) > prefix
    index = FragmentIndex(snapshot, _VerificationSession(), packet)
    assert (
        index.visible([HumanMessage(displayed)], displayed, initial_source_chars=source_chars) == []
    )


class RecordingVerifier(DemoVerifierModel):
    prompts: list = Field(default_factory=list)

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        self.prompts.append(messages)
        return super()._generate(messages, stop, run_manager, **kwargs)


def test_stages_share_fragment_identity_without_inheriting_reads_or_feedback(tmp_path):
    snapshot = make_snapshot(tmp_path)
    first = review(
        snapshot, DemoChatModel(), run_tests=True, memory="PRIVATE_REVIEW_FEEDBACK_MARKER"
    )
    model = RecordingVerifier()
    checked = verify_report(snapshot, first, model)
    main = checked["context"]["assembly"]
    verifier = checked["verification"]["context"]
    assert main["stage"] == "review" and verifier["stage"] == "verification"
    records = verifier["fragment_index"]["records"]
    assert_code_digests(snapshot, records)
    assert {r["path"] for r in records} == {"pricing.py"}
    assert {o["source"] for r in records for o in r["origins"]} == {"initial", "read_code"}
    assert "PRIVATE_REVIEW_FEEDBACK_MARKER" not in str(model.prompts)
    # Identical pinned code may share IDs, while each origin belongs to its own stage.
    ids = {r["id"] for r in main["fragment_index"]["records"]}
    assert ids.intersection(r["id"] for r in records)
    for request, messages in zip(verifier["requests"], model.prompts, strict=True):
        assert request["stage"] == "verification"
        assert request["source_fragments"]
        assert request["fragment_reference_chars"] <= BudgetPolicy().working_chars
        assert request["size"] <= request["limit"]
        for message in messages:
            if message.type == "system":
                for block in message.content_blocks:
                    if block.get("text", "").startswith(VERIFICATION_LEDGER_HEADER):
                        ledger = json.loads(block["text"][len(VERIFICATION_LEDGER_HEADER) :])
                        assert ledger["stage"] == "verification"


def test_reference_budget_drops_whole_entries_and_failures_do_not_create_fragments(tmp_path):
    snapshot = make_snapshot(tmp_path)
    report = candidate(snapshot)
    session = _VerificationSession()
    read_code = _tools(snapshot, report, 1, session)[0]
    read_code.invoke({"path": "not-allowed.py", "version": "head"})
    read_code.invoke({"path": "pricing.py", "version": "head", "start_line": 999})
    context = VerificationContext(
        snapshot,
        session,
        _packet(snapshot, report, 1),
        DemoVerifierModel(),
        ReceiptStateBackend(),
        BudgetPolicy(),
    )
    rendered, ids = context._references(700)
    ledger = json.loads(rendered[len(VERIFICATION_LEDGER_HEADER) :])
    assert len(rendered) <= 700 and ledger["omitted_references"] > 0
    assert ids == [r["id"] for r in ledger["fragments"]]
    assert all(
        o["source"] == "initial"
        for r in context.fragments.manifest()["records"]
        for o in r["origins"]
    )


def test_verifier_completed_index_and_requests_restore_in_separate_process(tmp_path):
    snapshot = make_snapshot(tmp_path)
    root = tmp_path / "runs"
    first = review(
        snapshot, DemoChatModel(), runs_dir=root, run_id="verify-process", run_tests=True
    )
    checked = verify_report(snapshot, first, DemoVerifierModel())
    code = """
import json, sys
from pathlib import Path
from pr_review_harness.snapshot import Snapshot
from pr_review_harness.verification import DemoVerifierModel, verify_report
report = json.loads(Path(sys.argv[1]).read_text())
m = report['run_manifest']
snapshot = Snapshot.load(Path(m['repo']), m['base_sha'], m['head_sha'])
restored = verify_report(snapshot, report, DemoVerifierModel())
print(json.dumps({'context': restored['verification']['context'],
                  'usage': restored['budget_usage']}))
"""
    child = subprocess.run(
        [sys.executable, "-c", code, str(root / "verify-process/review.json")],
        text=True,
        capture_output=True,
        check=True,
    )
    restored = json.loads(child.stdout)
    assert restored["context"] == checked["verification"]["context"]
    assert restored["usage"] == checked["budget_usage"]


def test_verifier_receipt_replay_rebuilds_origins_without_repeating_read(tmp_path, monkeypatch):
    snapshot = make_snapshot(tmp_path)
    report = review(
        snapshot,
        DemoChatModel(),
        runs_dir=tmp_path / "runs",
        run_id="verify-receipt",
        run_tests=True,
    )
    finish = RunStore.finish
    interrupted = False

    def interrupt(self, key, payload):
        nonlocal interrupted
        finish(self, key, payload)
        if not interrupted and payload.get("stage") == "verification_session":
            interrupted = True
            raise OSError("verifier receipt committed; graph not yet updated")

    monkeypatch.setattr(RunStore, "finish", interrupt)
    failed = verify_report(snapshot, report, DemoVerifierModel())
    assert failed["verification"]["status"] == "failed"
    assert failed["verification"]["context"]["requests"]
    monkeypatch.setattr(RunStore, "finish", finish)
    restored = verify_report(snapshot, failed, DemoVerifierModel())
    assert restored["verification"]["status"] == "completed"
    assert restored["budget_usage"]["tool_attempts"] == 6
    trace = restored["verification"]["trace"]
    assert [e["arguments"]["version"] for e in trace if e["tool"] == "read_code"] == [
        "head",
        "base",
    ]
    records = restored["verification"]["context"]["fragment_index"]["records"]
    assert {o["event"] for r in records for o in r["origins"] if o["source"] == "read_code"} == {
        0,
        1,
    }


class VerifierCompactionProbe(DemoVerifierModel):
    prompts: list = Field(default_factory=list)
    summary_calls: int = 0
    business_calls: int = 0

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        has_references = any(
            b.get("text", "").startswith(VERIFICATION_LEDGER_HEADER)
            for m in messages
            if m.type == "system"
            for b in m.content_blocks
        )
        if not has_references:
            self.summary_calls += 1
            message = AIMessage(content="Earlier verifier conversation omitted.")
        else:
            self.prompts.append(messages)
            self.business_calls += 1
            if self.business_calls <= 2:
                name = "read_code"
                args = {
                    "path": "pricing.py",
                    "version": "head" if self.business_calls == 1 else "base",
                    "end_line": 120,
                }
            else:
                name = "submit_verification"
                args = {
                    "decisions": [
                        {
                            "finding_index": 1,
                            "verdict": "supported",
                            "reason": "Scripted plumbing claim after two source reads.",
                        }
                    ]
                }
            message = AIMessage(
                content="",
                tool_calls=[
                    {"name": name, "args": args, "id": f"compact-verify-{self.business_calls}"}
                ],
            )
        return ChatResult(generations=[ChatGeneration(message=message)])


def test_verifier_native_summary_preserves_references_without_claiming_removed_code(tmp_path):
    padding = "# " + "x" * 77 + "\n"
    root, base = source_repo(tmp_path, {"pricing.py": "value = 1\n" + padding * 119})
    (root / "pricing.py").write_text("value = 2\n" + padding * 119)
    snapshot = Snapshot(root, base, commit(root, "head"))
    model = VerifierCompactionProbe()
    checked = verify_report(
        snapshot,
        candidate(snapshot),
        model,
        budget=BudgetPolicy(request_chars=24000, verify_read_chars=40000),
    )
    assert checked["verification"]["status"] == "completed", (
        checked["verification"].get("reason"),
        model.summary_calls,
        model.business_calls,
    )
    assert model.summary_calls >= 1
    context = checked["verification"]["context"]
    final = context["requests"][-1]
    assert not final["initial_display_present"] and final["initial_source_chars"] == 0
    assert final["fragment_reference_ids"]
    assert len(context["fragment_index"]["records"]) >= 4
    # References survive in the system message; only retained literal tool receipts
    # can still count as source in the post-summary request.
    index = FragmentIndex(
        snapshot,
        _VerificationSession(trace=checked["verification"]["trace"]),
        _packet(snapshot, candidate(snapshot), 1),
    )
    assert final["source_fragments"] == index.visible(model.prompts[-1], index.context.text)
    assert all(r["size"] <= r["limit"] for r in context["requests"])
