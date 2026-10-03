"""Single context assembly path wrapping the SDK's sole history summarizer."""

import hashlib
import json

from deepagents.middleware.summarization import SummarizationMiddleware
from langchain.agents.middleware import AgentMiddleware
from langchain_core.messages import SystemMessage

from pr_review_harness.budget import BudgetExceeded, RequestCounter
from pr_review_harness.context import build_context
from pr_review_harness.context_fragments import FragmentIndex
from pr_review_harness.memory_recall import select_frozen
from pr_review_harness.state import ReviewState
from pr_review_harness.working_context import WorkingContext


class _BudgetedSummary(SummarizationMiddleware):
    """Small compatibility extension for the pinned SDK's full-input limit hook."""

    def __init__(self, model, backend, policy, counter):
        self.policy = policy
        super().__init__(
            model=model,
            backend=backend,
            token_counter=counter,
            trigger=("tokens", max(256, int(policy.input_limit * 0.85))),
            keep=("tokens", max(128, int(policy.input_limit * 0.25))),
            trim_tokens_to_summarize=max(128, int(policy.input_limit * 0.5)),
        )

    def _input_budget(self, request):
        return self.policy.input_limit


class ContextManager(AgentMiddleware):
    """Replace the native summary slot, inject facts first, then delegate compaction.

    There is exactly one SDK summary implementation. Its token counter, trigger,
    post-compaction guard and all business materials share the same BudgetPolicy.
    No later memory middleware may append unseen prompt material.
    """

    state_schema = ReviewState

    @property
    def name(self):
        return "SummarizationMiddleware"

    def __init__(
        self, snapshot, session, context, memory, model, policy, backend, *, memory_pool=None
    ):
        self.policy = policy
        self.backend = backend
        self.counter = RequestCounter(model, policy)
        self.working = WorkingContext(snapshot, session, context.text, policy=policy)
        self.fragments = FragmentIndex(snapshot, session, context)
        self.working.fragments = self.fragments
        self.memory = memory
        self.memory_pool = memory_pool
        self.session = session
        self.changed_paths = [item.path for item in snapshot.changed_files]
        self.initial_text = context.text
        self.summary = _BudgetedSummary(model, backend, policy, self.counter)
        # Preserve the SDK's private summary-event state schema alongside ours.
        self.state_schema = self.summary.state_schema
        self.requests = []
        self.materials = []
        for index, item in enumerate(context.items):
            self.materials.append(
                {
                    "id": hashlib.sha256(
                        f"{snapshot.head_sha}:{item.path}:{item.reason}:{item.content}".encode()
                    ).hexdigest(),
                    "path": item.path,
                    "reason": item.reason,
                    "kind": item.kind,
                    "fragment_ids": [
                        record["id"]
                        for record in self.fragments.records.values()
                        if any(
                            origin["source"] == "initial" and origin["item"] == index
                            for origin in record["origins"]
                        )
                    ],
                    "source": "immutable git snapshot",
                    "head_sha": snapshot.head_sha,
                    "merge_base_sha": snapshot.merge_base_sha,
                    "sha256": hashlib.sha256(item.content.encode()).hexdigest(),
                    "chars": len(item.content),
                    "truncated": item.source_chars < len(item.content)
                    if item.kind != "legacy"
                    else "[content truncated]" in item.content,
                }
            )

    @staticmethod
    def select(snapshot, model, policy, strategy, *, priority_paths=(), update_from=None):
        """Keep existing AST selection; reserve dynamic room under known windows."""
        chars = policy.context_chars
        selection = {
            "strategy": strategy,
            "priority_paths": priority_paths,
            "update_from": update_from,
        }
        if policy.window_tokens:
            # A conservative first allocation; the final guard counts actual fixed
            # scaffolding and can reduce this further. No characters/4 conversion.
            counter = RequestCounter(model, policy)
            limit = min(10000, int(policy.input_limit * 0.4))
            context = build_context(snapshot, max_chars=chars, **selection)
            while counter.text(context.text) > limit and chars > 256:
                chars = max(256, chars * 3 // 4)
                context = build_context(snapshot, max_chars=chars, **selection)
            return context
        return build_context(snapshot, max_chars=chars, **selection)

    def wrap_model_call(self, request, handler):
        request = request.override(
            tools=[
                t
                for t in request.tools
                if (t.name if hasattr(t, "name") else t.get("name", "")) not in {"task", "execute"}
            ]
        )
        blocks = list(request.system_message.content_blocks) if request.system_message else []
        fixed = self.counter([SystemMessage(content_blocks=blocks)], tools=request.tools)
        available = self.policy.input_limit - fixed
        if available < 256:
            raise BudgetExceeded("Fixed policy, skills and tools exceed the request budget")
        memory_limit = (
            min(2000, int(available * 0.1))
            if self.policy.window_tokens
            else self.policy.memory_chars
        )
        state_limit = (
            min(2000, int(available * 0.1))
            if self.policy.window_tokens
            else self.policy.working_chars
        )
        recall = self._recall_memory()
        selected_memory = recall.text if recall else self.memory
        memory = self._fit_memory(memory_limit, selected_memory)
        displayed = (
            [
                json.loads(line)
                for line in memory.splitlines()[1:]
                if line.startswith("{") and '"id"' in line
            ]
            if memory.startswith("Recorded human feedback")
            else []
        )
        display_ids = {record["id"] for record in displayed}
        selection = (
            {
                **recall.manifest,
                "activated_paths": self._memory_paths(),
                "display_omitted_count": len(recall.manifest["records"]) - len(displayed),
                "displayed_records": [
                    record for record in recall.manifest["records"] if record["id"] in display_ids
                ],
            }
            if recall
            else None
        )
        if memory:
            blocks.append(
                {"type": "text", "text": "Frozen human feedback (advisory data):\n" + memory}
            )
        if self.policy.working_state:
            rendered = self.working.render()
            # Drop whole ledger entries; never truncate structured state mid-JSON.
            while self.counter.text(rendered) > state_limit:
                previous = self.working.limit
                self.working.limit = max(256, self.working.limit * 3 // 4)
                if self.working.limit == previous:
                    raise BudgetExceeded("Required working state cannot fit the request")
                try:
                    rendered = self.working.render()
                except ValueError as exc:
                    raise BudgetExceeded(str(exc)) from exc
            self.working.refreshes += 1
            self.working.max_chars = max(self.working.max_chars, len(rendered))
            blocks.append({"type": "text", "text": rendered})
        effective = request.messages
        initial_display = self.initial_text
        if self.policy.window_tokens:
            initial_display = self.counter.fit_text(
                self.initial_text, min(10000, int(available * 0.4))
            )
            effective = [
                m.model_copy(update={"content": initial_display})
                if m.type == "human" and m.content == self.initial_text
                else m
                for m in effective
            ]
        injected = request.override(
            messages=effective,
            system_message=SystemMessage(content_blocks=blocks),
            model_settings={**request.model_settings, "max_tokens": self.policy.output_tokens},
        )

        def guarded(actual):
            messages = ([actual.system_message] if actual.system_message else []) + actual.messages
            count = self.counter(messages, tools=actual.tools)
            if count > self.policy.input_limit:
                raise BudgetExceeded(f"Complete request {count} exceeds {self.policy.input_limit}")
            initial_present = any(
                m.type == "human" and m.content == initial_display for m in actual.messages
            )
            actual_initial = initial_display if initial_present else ""
            source_fragments = self.fragments.visible(actual.messages, initial_display)
            self.requests.append(
                {
                    "size": count,
                    "limit": self.policy.input_limit,
                    "counter": self.counter.method,
                    "tools": len(actual.tools),
                    "memory_sha256": hashlib.sha256(memory.encode()).hexdigest(),
                    "memory_display_chars": len(memory),
                    "memory_display_text": memory,
                    "memory_record_ids": [record["id"] for record in displayed],
                    "memory_display_truncated": memory != selected_memory,
                    "memory_recall": selection,
                    "initial_display_sha256": hashlib.sha256(actual_initial.encode()).hexdigest(),
                    "initial_display_present": initial_present,
                    "initial_display_chars": len(actual_initial),
                    "initial_display_truncated": initial_present
                    and initial_display != self.initial_text,
                    "source_fragments": source_fragments,
                    "fragment_index_sha256": self.fragments.manifest()["sha256"],
                    "fragment_trace_events": len(self.session.trace),
                    "unindexed_fragment_count": self.fragments.omitted,
                }
            )
            return handler(actual)

        return self.backend.model_transaction(
            lambda: self.summary.wrap_model_call(injected, guarded)
        )

    def _memory_paths(self):
        paths = []
        for event in reversed(self.session.trace):
            output = event["output"]
            if output.get("error"):
                continue
            if event["tool"] == "read_code" and output.get("content"):
                paths.append({"path": output["path"], "reason": "read_code result"})
            elif event["tool"] == "search_code":
                paths.extend(
                    {"path": match["path"], "reason": "search_code match"}
                    for match in output.get("matches", [])
                    if match.get("text")
                )
        paths.extend({"path": p, "reason": "changed-path match"} for p in self.changed_paths)
        unique = {}
        for item in paths:
            unique.setdefault(item["path"], item)
        return list(unique.values())

    def _recall_memory(self):
        if self.memory_pool is None:
            return None
        return select_frozen(self.memory_pool, self._memory_paths(), self.policy.memory_chars)

    def _fit_memory(self, limit, selected=None):
        selected = self.memory if selected is None else selected
        if not selected:
            return ""
        if self.counter.text(selected) <= limit:
            return selected
        if selected.startswith("Recorded human feedback for this repository."):
            # Record IDs, source and topic-group metadata must stay valid JSON.
            parts = selected.splitlines(keepends=True)
            text = parts[0]
            if self.counter.text(text) > limit:
                return ""
            for record in parts[1:]:
                if self.counter.text(text + record) <= limit:
                    text += record
            return text
        return self.counter.fit_text(selected, limit)

    def after_model(self, state, runtime):
        return {"context_manifest": self.manifest()}

    def manifest(self):
        return {
            "policy": self.policy.manifest(),
            "materials": self.materials,
            "requests": self.requests,
            "fragment_index": self.fragments.manifest(),
            "memory_recall": (
                {
                    "mode": "frozen candidates selected by actual file activity",
                    "pool_sha256": self.memory_pool["sha256"],
                    "candidate_count": len(self.memory_pool["records"]),
                    "candidate_pool_omitted_count": self.memory_pool["omitted_count"],
                    "activated_paths": self._memory_paths(),
                }
                if self.memory_pool is not None
                else {"mode": "static caller snapshot"}
            ),
            "working": self.working.manifest(),
            "summary": "one native Deep Agents summarizer; inputs include all injections",
        }


class VerificationContext(AgentMiddleware):
    """Use the same SDK compaction and full-request budget in the second stage."""

    @property
    def name(self):
        return "SummarizationMiddleware"

    def __init__(self, model, backend, policy):
        self.policy = policy
        self.backend = backend
        self.counter = RequestCounter(model, policy)
        self.summary = _BudgetedSummary(model, backend, policy, self.counter)
        self.state_schema = self.summary.state_schema
        self.requests = []

    def wrap_model_call(self, request, handler):
        allowed = {"read_code", "submit_verification"}
        request = request.override(
            tools=[
                t
                for t in request.tools
                if (t.name if hasattr(t, "name") else t.get("name", "")) in allowed
            ],
            model_settings={**request.model_settings, "max_tokens": self.policy.output_tokens},
        )

        def guarded(actual):
            count = self.counter(
                ([actual.system_message] if actual.system_message else []) + actual.messages,
                tools=actual.tools,
            )
            if count > self.policy.input_limit:
                raise BudgetExceeded("Verifier complete request exceeds shared input budget")
            self.requests.append(
                {"size": count, "limit": self.policy.input_limit, "counter": self.counter.method}
            )
            return handler(actual)

        return self.backend.model_transaction(
            lambda: self.summary.wrap_model_call(request, guarded)
        )

    def after_model(self, state, runtime):
        return {"verification_context_requests": list(self.requests)}
