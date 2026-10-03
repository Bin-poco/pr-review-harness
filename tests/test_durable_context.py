"""Exercise the actual graph, execution receipts, token guard and process boundary."""

import json
import subprocess
import sys
from dataclasses import replace
from uuid import uuid4

import pytest
from langchain_core.messages import AIMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from pydantic import Field
from test_runtime import make_snapshot

from pr_review_harness.budget import BudgetPolicy, RequestCounter
from pr_review_harness.checks import CheckRunner
from pr_review_harness.memory import MemoryStore
from pr_review_harness.persistence import ModelAccounting, RunStore, digest
from pr_review_harness.runtime import DemoChatModel, ReviewFailure, review
from pr_review_harness.verification import DemoVerifierModel, verify_report


class InterruptibleModel(DemoChatModel):
    fail: bool = True
    calls: int = 0

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        self.calls += 1
        if self.fail and self.calls == 3:
            raise RuntimeError("simulated provider interruption after completed check")
        return super()._generate(messages, stop, run_manager, **kwargs)


def test_resume_keeps_evidence_budget_and_frozen_memory(tmp_path, monkeypatch):
    snapshot = make_snapshot(tmp_path)
    memory = MemoryStore(tmp_path / "memory.sqlite3")
    record = memory.add(snapshot.repo_id, "Original maintainer feedback", "previous PR")
    captured = memory.recall_snapshot(snapshot.repo_id, ["pricing.py"])
    options = {"runs_dir": tmp_path / "runs", "run_id": "resume", "run_tests": True}
    with pytest.raises(ReviewFailure):
        review(snapshot, InterruptibleModel(), memory=captured, **options)
    memory.revoke(snapshot.repo_id, record, reason="changed between interruption and resume")

    def should_not_repeat(*args, **kwargs):
        raise AssertionError("A completed check must not execute again")

    monkeypatch.setattr(CheckRunner, "_run_version", should_not_repeat)
    report = review(
        snapshot, InterruptibleModel(fail=False), memory="New feedback", resume=True, **options
    )
    assert report["evidence"][0]["id"] == "check-001"
    assert report["evidence"][0]["head"]["status"] == "failed"
    assert report["context"]["memory_snapshot"]["text"] == captured.text
    assert report["context"]["additional_read_chars"] > 0
    assert report["budget_usage"]["model_attempts"] == 4
    assert report["budget_usage"]["tool_attempts"] == 3
    assert len(report["trace"]) == 3
    assert report["resumed"]


def test_completed_run_restores_in_a_new_process_without_model_calls(tmp_path):
    snapshot = make_snapshot(tmp_path)
    root = tmp_path / "runs"
    first = review(snapshot, DemoChatModel(), runs_dir=root, run_id="process", run_tests=True)
    code = """
import json, sys
from pathlib import Path
from pr_review_harness.persistence import RunStore
from pr_review_harness.snapshot import Snapshot
from pr_review_harness.runtime import DemoChatModel, review
from pr_review_harness.budget import BudgetPolicy
root=Path(sys.argv[1]); manifest,_=RunStore(root,"process").load()
snapshot=Snapshot.load(Path(manifest["repo"]),manifest["base_sha"],manifest["head_sha"])
report=review(snapshot,DemoChatModel(),runs_dir=root,run_id="process",resume=True,
              run_tests=True,budget=BudgetPolicy(**manifest["budget"]))
print(json.dumps({"usage":report["budget_usage"],"evidence":report["evidence"],
                  "findings":report["findings"]}))
"""
    child = subprocess.run(
        [sys.executable, "-c", code, str(root)], text=True, capture_output=True, check=True
    )
    resumed = json.loads(child.stdout)
    assert resumed["usage"] == first["budget_usage"]
    assert resumed["evidence"] == first["evidence"]
    assert resumed["findings"] == first["findings"]


def test_resume_rejects_changed_policy_or_corrupted_artifacts(tmp_path):
    snapshot = make_snapshot(tmp_path)
    root = tmp_path / "runs"
    policy = BudgetPolicy()
    review(snapshot, DemoChatModel(), runs_dir=root, run_id="strict", run_tests=True, budget=policy)
    with pytest.raises(ValueError, match="Cannot resume"):
        review(
            snapshot,
            DemoChatModel(),
            runs_dir=root,
            run_id="strict",
            run_tests=True,
            budget=replace(policy, read_chars=policy.read_chars + 1),
            resume=True,
        )
    artifact = root / "strict/artifacts.json"
    value = json.loads(artifact.read_text())
    value["memory"]["text"] = "tampered"
    artifact.write_text(json.dumps(value))
    with pytest.raises(ValueError, match="artifacts"):
        review(
            snapshot, DemoChatModel(), runs_dir=root, run_id="strict", run_tests=True, resume=True
        )


def test_unknown_execution_requires_explicit_retry(tmp_path, monkeypatch):
    snapshot = make_snapshot(tmp_path)
    options = {"runs_dir": tmp_path / "runs", "run_id": "unknown", "run_tests": True}
    original = RunStore.finish
    interrupted = False

    def interrupt_after_check(self, key, payload):
        nonlocal interrupted
        if not interrupted and payload.get("session", {}).get("evidence"):
            interrupted = True
            raise OSError("simulated crash between execution and receipt commit")
        return original(self, key, payload)

    monkeypatch.setattr(RunStore, "finish", interrupt_after_check)
    with pytest.raises(ReviewFailure):
        review(snapshot, DemoChatModel(), **options)
    monkeypatch.setattr(RunStore, "finish", original)
    with pytest.raises(ReviewFailure) as failed:
        review(snapshot, DemoChatModel(), **options, resume=True)
    assert failed.value.partial["execution_unknown"] is True
    report = review(snapshot, DemoChatModel(), **options, resume=True, retry_unknown=True)
    assert len(report["evidence"]) == 1
    assert report["budget_usage"]["tool_attempts"] == 4


class BatchModel(DemoChatModel):
    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        from langchain_core.messages import ToolMessage

        completed = [m for m in messages if isinstance(m, ToolMessage)]
        if not completed:
            calls = [
                {
                    "name": "read_code",
                    "args": {"path": "pricing.py", "version": version},
                    "id": f"batch-{version}",
                }
                for version in ("head", "base")
            ]
        else:
            calls = [{"name": "submit_review", "args": {"findings": []}, "id": "batch-submit"}]
        return ChatResult(
            generations=[ChatGeneration(message=AIMessage(content="", tool_calls=calls))]
        )


def test_tool_batch_merges_serial_facts_without_losing_reads(tmp_path):
    snapshot = make_snapshot(tmp_path)
    report = review(snapshot, BatchModel(), runs_dir=tmp_path / "runs", run_id="batch")
    assert len(report["trace"]) == 3
    assert {r["arguments"]["version"] for r in report["trace"][:2]} == {"head", "base"}
    assert report["context"]["additional_read_chars"] == sum(
        len(e["output"]["content"]) for e in report["trace"][:2]
    )


class PromptRecorder(DemoChatModel):
    prompts: list = Field(default_factory=list)

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        self.prompts.append(messages)
        return super()._generate(messages, stop, run_manager, **kwargs)


def test_complete_token_guard_counts_memory_skills_and_schemas(tmp_path):
    snapshot = make_snapshot(tmp_path)
    model = PromptRecorder()
    policy = BudgetPolicy(window_tokens=65536, window_source="test explicit")
    report = review(snapshot, model, memory="CONFIRMED_GUIDANCE", run_tests=True, budget=policy)
    requests = report["context"]["assembly"]["requests"]
    assert len(requests) == len(model.prompts) == 3
    assert all(r["size"] <= policy.input_limit for r in requests)
    assert all(r["tools"] > 4 for r in requests)  # SDK scratch tools are counted too.
    assert all("CONFIRMED_GUIDANCE" in str(p) for p in model.prompts)
    assert "python-boundary-regressions" in str(model.prompts[0])
    assert "Harness working state" in str(model.prompts[0])
    assert report["context"]["assembly"]["policy"]["window_source"] == "test explicit"


def test_irreducible_small_window_never_sends_to_model(tmp_path):
    snapshot = make_snapshot(tmp_path)
    model = PromptRecorder()
    policy = BudgetPolicy(window_tokens=8192, output_tokens=2048)
    with pytest.raises(ReviewFailure, match="BudgetExceeded"):
        review(snapshot, model, budget=policy)
    assert model.prompts == []


def test_actual_returned_range_excludes_partial_line(tmp_path):
    snapshot = make_snapshot(tmp_path)
    policy = BudgetPolicy(read_chars=12)
    report = review(snapshot, DemoChatModel(), run_tests=True, budget=policy)
    read = report["trace"][0]["output"]
    assert read["truncated"]
    assert read["returned_range"] is None  # first line is longer than 12 characters
    assert report["context"]["additional_read_chars"] == 12


def test_tool_schema_is_counted_for_openai_adapter_without_network():
    from langchain_core.messages import HumanMessage
    from langchain_openai import ChatOpenAI

    model = ChatOpenAI(model="gpt-4o", api_key="offline-placeholder")
    counter = RequestCounter(model, BudgetPolicy(window_tokens=32768))
    messages = [HumanMessage(content="review")]
    plain = counter(messages)
    schema = {
        "type": "function",
        "function": {
            "name": "large_schema",
            "description": "x" * 10000,
            "parameters": {"type": "object", "properties": {}},
        },
    }
    assert counter(messages, tools=[schema]) > plain + 1000


def test_attempt_budget_does_not_reset_across_new_accounting_objects(tmp_path):
    store = RunStore(tmp_path, "budget")
    policy = BudgetPolicy(model_calls=2, total_model_calls=3)
    for _ in range(2):
        ModelAccounting(policy, store).on_chat_model_start({}, [], run_id=uuid4())
    from pr_review_harness.budget import BudgetExceeded

    with pytest.raises(BudgetExceeded, match="review model attempt"):
        ModelAccounting(policy, store).on_chat_model_start({}, [], run_id=uuid4())
    assert store.budget_usage()["model_attempts"] == 2


def test_verifier_shares_global_budget_and_restores_completed_state(tmp_path):
    snapshot = make_snapshot(tmp_path)
    report = review(
        snapshot, DemoChatModel(), runs_dir=tmp_path / "runs", run_id="verify", run_tests=True
    )
    verified = verify_report(snapshot, report, DemoVerifierModel())
    assert verified["verification"]["status"] == "completed"
    assert verified["budget_usage"]["model_attempts"] == 6
    again = verify_report(snapshot, verified, DemoVerifierModel())
    assert again["budget_usage"]["model_attempts"] == 6
    assert again["verification"]["trace"] == verified["verification"]["trace"]
    assert again["verification"]["verdicts"] == verified["verification"]["verdicts"]


def test_run_lock_prevents_two_active_writers(tmp_path):
    store = RunStore(tmp_path, "locked")
    with store.locked(), pytest.raises(ValueError, match="already active"):
        with RunStore(tmp_path, "locked").locked():
            pass


def test_tool_receipt_identity_includes_stage():
    # A receipt represents one emitted invocation, never just a tool name/path.
    assert digest({"stage": "review_session", "call": {"id": "x"}}) != digest(
        {"stage": "verification_session", "call": {"id": "x"}}
    )


class CompactionProbe(DemoChatModel):
    prompts: list = Field(default_factory=list)
    summary_calls: int = 0

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        from pr_review_harness.working_context import LEDGER_HEADER

        system = next((m for m in messages if m.type == "system"), None)
        blocks = system.content_blocks if system else []
        ledger = next(
            (b["text"] for b in blocks if b.get("text", "").startswith(LEDGER_HEADER)), None
        )
        if ledger is None:
            self.summary_calls += 1
            return ChatResult(
                generations=[ChatGeneration(message=AIMessage(content="Earlier chat omitted."))]
            )
        self.prompts.append(messages)
        state = json.loads(ledger[len(LEDGER_HEADER) :])
        if state["tool_calls"] == 0:
            name, args = "read_code", {"path": "pricing.py"}
        elif not state["evidence"]:
            name, args = "run_check", {"kind": "syntax", "path": "pricing.py"}
        else:
            name, args = "submit_review", {"findings": []}
        return ChatResult(
            generations=[
                ChatGeneration(
                    message=AIMessage(
                        content="",
                        tool_calls=[
                            {"name": name, "args": args, "id": f"compact-{state['tool_calls']}"}
                        ],
                    )
                )
            ]
        )


def test_unified_context_survives_native_compaction_and_restores_files(tmp_path, monkeypatch):
    import pr_review_harness.runtime as runtime

    graphs = []
    factory = runtime.create_deep_agent

    def capture_graph(*args, **kwargs):
        graph = factory(*args, **kwargs)
        graphs.append(graph)
        return graph

    monkeypatch.setattr(runtime, "create_deep_agent", capture_graph)
    snapshot = make_snapshot(tmp_path)
    original = CheckRunner._run_version

    def verbose_result(self, *args):
        result = original(self, *args)
        return replace(result, output="x" * 7000)

    monkeypatch.setattr(CheckRunner, "_run_version", verbose_result)
    policy = BudgetPolicy(request_chars=24000)
    model = CompactionProbe()
    report = review(
        snapshot,
        model,
        budget=policy,
        runs_dir=tmp_path / "runs",
        run_id="compact",
        memory="CONFIRMED_MEMORY_AFTER_COMPACTION",
    )
    assert model.summary_calls >= 1
    assert report["budget_usage"]["model_attempts"] > len(model.prompts)
    assert "check-001" in str(model.prompts[-1])
    assert "CONFIRMED_MEMORY_AFTER_COMPACTION" in str(model.prompts[-1])
    assembly = report["context"]["assembly"]
    assert not assembly["requests"][-1]["initial_display_present"]
    assert assembly["requests"][-1]["initial_display_chars"] == 0
    ledger = assembly["working"]["final_state"]
    assert '"fragments"' in ledger
    # Fragment IDs survive as references, while summarized-away source does not
    # appear in the post-summary request's literal source visibility list.
    if not any(m.type == "tool" and m.name == "read_code" for m in model.prompts[-1]):
        assert assembly["requests"][-1]["source_fragments"] == []
    assert all(r["size"] <= policy.input_limit for r in report["context"]["assembly"]["requests"])
    restored = review(
        snapshot,
        CompactionProbe(),
        budget=policy,
        runs_dir=tmp_path / "runs",
        run_id="compact",
        resume=True,
    )
    assert restored["evidence"] == report["evidence"]
    assert restored["context"]["assembly"]["fragment_index"] == assembly["fragment_index"]
    assert restored["budget_usage"] == report["budget_usage"]
    from langgraph.checkpoint.sqlite import SqliteSaver

    # The pinned SDK stores files in DeltaChannel history, not directly in
    # checkpoint.channel_values. Reconstruct through the graph's real channels.
    with SqliteSaver.from_conn_string(str(tmp_path / "runs/compact/checkpoint.sqlite3")) as saver:
        graphs[-1].checkpointer = saver
        saved = graphs[-1].get_state({"configurable": {"thread_id": "compact"}})
        event = saved.values["_summarization_event"]
        history = saved.values["files"][event["file_path"]]
        assert "Immutable PR review context" in str(history)
        assert saved.values["review_session"]["evidence"][0]["id"] == "check-001"
        assert saved.values["files"]["/memories/repository.md"]


def test_head_change_refuses_to_mix_run_state(tmp_path):
    from pr_review_harness.snapshot import Snapshot

    snapshot = make_snapshot(tmp_path)
    options = {"runs_dir": tmp_path / "runs", "run_id": "sha", "run_tests": True}
    review(snapshot, DemoChatModel(), **options)
    changed = Snapshot.load(snapshot.repo, snapshot.base_sha, snapshot.base_sha)
    with pytest.raises(ValueError, match="Cannot resume"):
        review(changed, DemoChatModel(), **options, resume=True)


def test_completed_receipt_replays_without_reexecuting_tool(tmp_path, monkeypatch):
    snapshot = make_snapshot(tmp_path)
    options = {"runs_dir": tmp_path / "runs", "run_id": "receipt", "run_tests": True}
    original = RunStore.finish
    interrupted = False

    def save_then_interrupt(self, key, payload):
        nonlocal interrupted
        original(self, key, payload)
        if not interrupted and payload.get("session", {}).get("evidence"):
            interrupted = True
            raise OSError("simulated interruption after receipt, before graph update")

    monkeypatch.setattr(RunStore, "finish", save_then_interrupt)
    with pytest.raises(ReviewFailure):
        review(snapshot, DemoChatModel(), **options)
    monkeypatch.setattr(RunStore, "finish", original)

    def forbid_execution(*args, **kwargs):
        raise AssertionError("Completed receipt must be replayed")

    monkeypatch.setattr(CheckRunner, "_run_version", forbid_execution)
    report = review(snapshot, DemoChatModel(), **options, resume=True)
    assert len(report["trace"]) == 3
    assert report["evidence"][0]["id"] == "check-001"
    assert report["budget_usage"]["tool_attempts"] == 3


class ScratchWriterModel(DemoChatModel):
    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        from langchain_core.messages import ToolMessage

        writes = [m for m in messages if isinstance(m, ToolMessage) and m.name == "write_file"]
        reads = [m for m in messages if isinstance(m, ToolMessage) and m.name == "read_file"]
        if not writes:
            name, args = "write_file", {"file_path": "/notes.txt", "content": "durable scratch"}
        elif not reads:
            name, args = "read_file", {"file_path": "/notes.txt"}
        else:
            assert "durable scratch" in str(reads[-1].content)
            name, args = "submit_review", {"findings": []}
        return ChatResult(
            generations=[
                ChatGeneration(
                    message=AIMessage(
                        content="",
                        tool_calls=[{"name": name, "args": args, "id": "scratch-" + name}],
                    )
                )
            ]
        )


def test_file_write_receipt_restores_pending_backend_changes(tmp_path, monkeypatch):
    snapshot = make_snapshot(tmp_path)
    original = RunStore.finish
    interrupted = False

    def crash_before_graph_commit(self, key, payload):
        nonlocal interrupted
        original(self, key, payload)
        if not interrupted and "/notes.txt" in payload.get("update", {}).get("files", {}):
            interrupted = True
            raise OSError("file receipt committed; graph files not yet committed")

    monkeypatch.setattr(RunStore, "finish", crash_before_graph_commit)
    options = {"runs_dir": tmp_path / "runs", "run_id": "scratch"}
    with pytest.raises(ReviewFailure):
        review(snapshot, ScratchWriterModel(), **options)
    assert interrupted
    monkeypatch.setattr(RunStore, "finish", original)
    report = review(snapshot, ScratchWriterModel(), resume=True, **options)
    assert report["findings"] == []
    assert report["budget_usage"]["tool_attempts"] == 3
    read = next(m for m in report["messages"] if m.get("name") == "read_file")
    assert "durable scratch" in read["content"]


class InterruptAfterSummary(CompactionProbe):
    fail: bool = True

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        from pr_review_harness.working_context import LEDGER_HEADER

        is_review = any(
            block.get("text", "").startswith(LEDGER_HEADER)
            for message in messages
            if message.type == "system"
            for block in message.content_blocks
        )
        if self.fail and self.summary_calls and is_review:
            raise RuntimeError("provider interrupted after summary offload")
        return super()._generate(messages, stop, run_manager, **kwargs)


def test_interruption_after_summary_offload_remains_resumable(tmp_path, monkeypatch):
    snapshot = make_snapshot(tmp_path)
    original = CheckRunner._run_version

    def verbose_result(self, *args):
        return replace(original(self, *args), output="x" * 7000)

    monkeypatch.setattr(CheckRunner, "_run_version", verbose_result)
    options = {
        "runs_dir": tmp_path / "runs",
        "run_id": "summary-crash",
        "budget": BudgetPolicy(request_chars=24000),
    }
    with pytest.raises(ReviewFailure) as error:
        review(snapshot, InterruptAfterSummary(), **options)
    assert error.value.partial["evidence"][0]["id"] == "check-001"
    report = review(snapshot, InterruptAfterSummary(fail=False), resume=True, **options)
    assert report["findings"] == []
    assert (
        report["budget_usage"]["model_attempts"]
        > error.value.partial["budget_usage"]["model_attempts"]
    )
    assert report["budget_usage"]["tool_attempts"] == 3
