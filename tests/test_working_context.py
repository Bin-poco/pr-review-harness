"""Verify that factual working state survives actual native history compaction."""

import json

from deepagents import create_deep_agent
from deepagents.backends import StateBackend
from deepagents.middleware.summarization import SummarizationMiddleware
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from pydantic import Field
from test_runtime import make_snapshot

from pr_review_harness.checks import CheckRunner
from pr_review_harness.models import ReviewSession
from pr_review_harness.runtime import ReviewToolScope, _review_tools
from pr_review_harness.working_context import LEDGER_HEADER, LEDGER_LIMIT, WorkingContext


class EmptySummaryModel(BaseChatModel):
    calls: int = 0

    @property
    def _llm_type(self):
        return "deliberately-lossy-test-summary"

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        self.calls += 1
        return ChatResult(
            generations=[ChatGeneration(message=AIMessage(content="Earlier chat omitted."))]
        )


class LedgerProbe(BaseChatModel):
    states: list[dict] = Field(default_factory=list)
    prompts: list[str] = Field(default_factory=list)

    @property
    def _llm_type(self):
        return "working-state-probe"

    def bind_tools(self, tools, *, tool_choice=None, **kwargs):
        return self

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        system = next(message for message in messages if message.type == "system")
        block = next(
            block
            for block in system.content_blocks
            if block.get("text", "").startswith(LEDGER_HEADER)
        )
        state = json.loads(block["text"][len(LEDGER_HEADER) :])
        self.states.append(state)
        self.prompts.append(str(messages))
        if state["tool_calls"] == 0:
            name, args = "read_code", {"path": "pricing.py", "start_line": 3, "end_line": 5}
        elif state["tool_calls"] == 1:
            name, args = "run_check", {"kind": "syntax", "path": "pricing.py"}
        else:
            name, args = "submit_review", {"findings": []}
        return ChatResult(
            generations=[
                ChatGeneration(
                    message=AIMessage(
                        content="",
                        tool_calls=[{"name": name, "args": args, "id": f"call-{len(self.states)}"}],
                    )
                )
            ]
        )


def test_working_state_survives_native_compaction(tmp_path, monkeypatch):
    from threading import Lock

    import deepagents.graph as graph

    summary = EmptySummaryModel()
    monkeypatch.setattr(
        graph,
        "create_summarization_middleware",
        lambda model, backend: SummarizationMiddleware(
            model=summary, backend=backend, trigger=("messages", 4), keep=("messages", 2)
        ),
    )
    snapshot = make_snapshot(tmp_path)
    session = ReviewSession()
    working = WorkingContext(snapshot, session, "initial context")
    model = LedgerProbe()
    agent = create_deep_agent(
        model=model,
        backend=StateBackend(),
        tools=_review_tools(snapshot, session, CheckRunner(snapshot), Lock()),
        middleware=[ReviewToolScope(session), working],
    )
    agent.invoke({"messages": [{"role": "user", "content": "UNIQUE_INITIAL_DETAIL"}]})
    assert summary.calls >= 1
    assert "UNIQUE_INITIAL_DETAIL" not in model.prompts[-1]
    assert session.findings == []
    final = model.states[-1]
    assert final["head_sha"] == snapshot.head_sha
    assert final["evidence"][0]["id"] == "check-001"
    assert final["evidence"][0]["head_status"] == "passed"
    assert final["reads"][0]["request"]["start_line"] == 3
    assert final["reads"][0]["request"]["end_line"] == 5
    assert working.refreshes == 3


def test_working_state_is_bounded_and_reports_omissions(tmp_path):
    snapshot = make_snapshot(tmp_path)
    session = ReviewSession()
    for index in range(20):
        session.trace.append(
            {
                "tool": "read_code",
                "arguments": {"path": "x" * 2000, "start_line": index + 1, "end_line": index + 1},
                "output": {"truncated": True},
            }
        )
    working = WorkingContext(snapshot, session, "initial")
    rendered = working.render()
    state = json.loads(rendered[len(LEDGER_HEADER) :])
    assert len(rendered) <= LEDGER_LIMIT
    assert state["omitted_reads"] + len(state["reads"]) == 20
    assert all(item["truncated"] for item in state["reads"])
    assert state["head_sha"] == snapshot.head_sha


class FeedbackEditProbe(BaseChatModel):
    calls: int = 0

    @property
    def _llm_type(self):
        return "read-only-feedback-probe"

    def bind_tools(self, tools, *, tool_choice=None, **kwargs):
        return self

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        self.calls += 1
        if self.calls == 1:
            name, args = (
                "edit_file",
                {
                    "file_path": "/scratch/../memories/repository.md",
                    "old_string": "Confirmed rule",
                    "new_string": "Invented rule",
                },
            )
        else:
            name, args = "submit_review", {"findings": []}
        return ChatResult(
            generations=[
                ChatGeneration(
                    message=AIMessage(
                        content="",
                        tool_calls=[{"name": name, "args": args, "id": f"edit-{self.calls}"}],
                    )
                )
            ]
        )


def test_agent_cannot_edit_confirmed_feedback(tmp_path):
    from threading import Lock

    from deepagents.backends.utils import create_file_data
    from langchain_core.messages import ToolMessage

    snapshot = make_snapshot(tmp_path)
    session = ReviewSession()
    agent = create_deep_agent(
        model=FeedbackEditProbe(),
        backend=StateBackend(),
        memory=["/memories/repository.md"],
        tools=_review_tools(snapshot, session, CheckRunner(snapshot), Lock()),
        middleware=[ReviewToolScope(session)],
    )
    result = agent.invoke(
        {
            "messages": [{"role": "user", "content": "Review this PR"}],
            "files": {"/memories/repository.md": create_file_data("Confirmed rule")},
        }
    )
    assert result["files"]["/memories/repository.md"]["content"] == "Confirmed rule"
    assert any(
        "read-only" in str(message.content)
        for message in result["messages"]
        if isinstance(message, ToolMessage)
    )
