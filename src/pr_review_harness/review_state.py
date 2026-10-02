"""Serial tool transactions publish facts to graph state and durable receipts."""

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import replace
from threading import RLock

from deepagents.backends import StateBackend
from langchain.agents.middleware import AgentMiddleware
from langchain.agents.middleware.types import ExtendedModelResponse
from langchain_core.messages import ToolMessage
from langgraph.types import Command

from pr_review_harness.budget import BudgetExceeded
from pr_review_harness.persistence import digest
from pr_review_harness.state import ReviewState, dump_session, load_session


class ExecutionUnknown(RuntimeError):
    """A tool started but has no complete durable result; explicit retry is required."""


class ReceiptStateBackend(StateBackend):
    """Buffer SDK file-tool writes until their durable receipt is ready.

    Partial CONFIG_KEY_SEND writes can make the graph treat an interrupted tool
    as completed. File tools therefore publish through the same Command as their
    result. Summary offloads similarly publish with the successful model result.
    These private hooks are compatibility points for pinned Deep Agents 0.7.21.
    """

    def __init__(self):
        super().__init__()
        self.pending = ContextVar("review_file_transaction", default=None)

    @contextmanager
    def transaction(self):
        delta = {}
        token = self.pending.set(delta)
        try:
            yield delta
        finally:
            self.pending.reset(token)

    def _send_files_update(self, update):
        delta = self.pending.get()
        if delta is None:
            super()._send_files_update(update)
        else:
            delta.update(update)

    def _read_files(self):
        files = dict(super()._read_files())
        for path, value in (self.pending.get() or {}).items():
            if value is None:
                files.pop(path, None)
            else:
                files[path] = value
        return files

    def model_transaction(self, handler):
        with self.transaction() as delta:
            result = handler()
        if not delta:
            return result
        if isinstance(result, ExtendedModelResponse):
            command = result.command or Command(update={})
            update = {**command.update, "files": {**command.update.get("files", {}), **delta}}
            return replace(result, command=replace(command, update=update))
        return ExtendedModelResponse(
            model_response=result, command=Command(update={"files": delta})
        )


class ReviewFacts(AgentMiddleware):
    state_schema = ReviewState

    def __init__(
        self,
        session,
        checks,
        policy,
        store=None,
        *,
        serializer=dump_session,
        loader=load_session,
        state_key="review_session",
        backend=None,
    ):
        self.serializer = serializer
        self.loader = loader
        self.state_key = state_key
        self.backend = backend
        self.session = session
        self.checks = checks
        self.policy = policy
        self.store = store
        self.lock = RLock()
        self.attempts = 0
        self.stage_limit = policy.tool_calls

    def hydrate(self, value):
        if value and value.get("sequence", 0) >= self.session.sequence:
            self.loader(self.session, value)
        if self.checks is not None:
            self.checks.cache = {(e.kind, e.path): e for e in self.session.evidence}

    def wrap_tool_call(self, request, handler):
        with self.lock:
            self.hydrate(request.state.get(self.state_key))
            key = "tool-" + digest({"stage": self.state_key, "call": request.tool_call})
            retry_key = None
            if self.store:
                old = self.store.receipt(key)
                if old and old["status"] == "completed":
                    saved = old["payload"]
                    self.hydrate(saved["session"])
                    update = dict(saved["update"])
                    update["messages"] = [ToolMessage(**m) for m in update.get("messages", [])]
                    return Command(update=update)
                if old and old["status"] == "started":
                    raise ExecutionUnknown(
                        "Tool execution_unknown: resume with --retry-unknown to explicitly rerun"
                    )
                if not old:
                    self.store.start(
                        key,
                        "tool",
                        self.policy.total_tool_calls,
                        payload={"stage": self.state_key},
                        stage_limit=self.stage_limit,
                    )
                else:
                    # Keep the previous unknown attempt in accounting and reserve a
                    # separate retry. The original key remains the replay identity.
                    from uuid import uuid4

                    retry_key = key + "-retry-" + str(uuid4())
                    self.store.start(
                        retry_key,
                        "tool",
                        self.policy.total_tool_calls,
                        payload={"stage": self.state_key},
                        stage_limit=self.stage_limit,
                    )
            elif self.attempts >= self.policy.total_tool_calls:
                raise BudgetExceeded("Global tool call budget exhausted")
            self.attempts += 1
            filesystem_change = self.backend is not None and request.tool_call["name"] in {
                "write_file",
                "edit_file",
                "delete_file",
                "delete",
            }
            file_delta = {}
            if (
                getattr(self.session, "findings", None) is not None
                or getattr(self.session, "decisions", None) is not None
            ):
                result = ToolMessage(
                    content="Review already submitted; no further tools executed.",
                    tool_call_id=request.tool_call["id"],
                    name=request.tool_call["name"],
                )
            else:
                if filesystem_change:
                    with self.backend.transaction() as file_delta:
                        result = handler(request)
                else:
                    result = handler(request)
            self.session.sequence += 1
            session = self.serializer(self.session)
            if isinstance(result, Command):
                update = {**result.update, self.state_key: session}
                command = replace(result, update=update)
            else:
                update = {"messages": [result], self.state_key: session}
                command = Command(update=update)
            if file_delta:
                update["files"] = {**update.get("files", {}), **file_delta}
            if self.store:
                saved = {
                    **update,
                    "messages": [m.model_dump(mode="json") for m in update.get("messages", [])],
                }
                self.store.finish(
                    key, {"stage": self.state_key, "session": session, "update": saved}
                )
                if retry_key:
                    self.store.finish(
                        retry_key, {"stage": self.state_key, "session": session, "update": saved}
                    )
            return command
