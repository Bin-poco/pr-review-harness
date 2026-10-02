"""Checkpointed, bounded correction of missing or invalid final submissions."""

from langchain.agents.middleware import AgentMiddleware, hook_config
from langchain_core.messages import AIMessage, SystemMessage, ToolMessage

from pr_review_harness.persistence import response_diagnostics
from pr_review_harness.state import ReviewState


class SubmissionGuard(AgentMiddleware):
    state_schema = ReviewState

    def __init__(self, session, policy, accounting, facts):
        self.session = session
        self.policy = policy
        self.accounting = accounting
        self.facts = facts
        self.control = self.empty()

    @staticmethod
    def empty():
        return {
            "repair_requests": 0,
            "force_submit": False,
            "observations": [],
            "seen_rejections": [],
            "observed_trace_count": 0,
            "stop_reason": None,
        }

    def hydrate(self, state):
        saved = state.get("submission_control") or self.control
        self.control = {
            **self.empty(),
            **saved,
            "observations": list(saved.get("observations", [])),
            "seen_rejections": list(saved.get("seen_rejections", [])),
        }

    def _repair(self, reason):
        """Reserve one correction; ordinary review and global limits still apply."""
        self.control["last_failure"] = reason
        used = (
            self.facts.store.budget_usage()["tool_attempts"]
            if self.facts.store
            else (self.facts.attempts)
        )
        stage_used = (
            self.facts.store.stage_attempts("tool", "review_session")
            if (self.facts.store)
            else self.facts.attempts
        )
        if self.control["repair_requests"] >= self.policy.submission_repairs:
            stop = "repair_limit_reached"
        elif self.accounting.remaining() < 1:
            stop = "model_budget_exhausted"
        elif min(self.policy.tool_calls - stage_used, self.policy.total_tool_calls - used) < 1:
            stop = "tool_budget_exhausted"
        else:
            self.control["repair_requests"] += 1
            self.control["force_submit"] = True
            self.control["stop_reason"] = None
            return True
        self.control["stop_reason"] = stop
        return False

    @hook_config(can_jump_to=["end"])
    def before_model(self, state, runtime):
        self.hydrate(state)
        self.facts.hydrate(state.get("review_session"))
        if self.session.findings is not None:
            return None
        errors = []
        messages = state.get("messages", [])
        latest = next((m for m in reversed(messages) if isinstance(m, AIMessage)), None)
        response_id = latest.id if latest else "unknown"
        # Only examine tool replies following the latest model response.
        for message in reversed(messages):
            if isinstance(message, AIMessage):
                break
            if (
                isinstance(message, ToolMessage)
                and message.name == "submit_review"
                and (
                    message.status == "error"
                    and f"{response_id}:{message.tool_call_id}"
                    not in (self.control["seen_rejections"])
                )
            ):
                self.control["seen_rejections"].append(f"{response_id}:{message.tool_call_id}")
                errors.append("submission_schema_error")
        new_trace = self.session.trace[self.control["observed_trace_count"] :]
        self.control["observed_trace_count"] = len(self.session.trace)
        if any(t["tool"] == "submit_review" and not t["output"].get("accepted") for t in new_trace):
            errors.append("submission_validation_error")
        if not errors:
            return None
        self.control["observations"].append({"kind": "tool_rejection", "reasons": errors})
        retry = self._repair(errors[0])
        return {"submission_control": self.control, **({} if retry else {"jump_to": "end"})}

    @hook_config(can_jump_to=["model", "end"])
    def after_model(self, state, runtime):
        self.hydrate(state)
        message = state["messages"][-1]
        if not isinstance(message, AIMessage):
            return None
        observation = response_diagnostics(message)
        observation["kind"] = "model_response"
        self.control["observations"].append(observation)
        if message.tool_calls:
            return {"submission_control": self.control}
        if observation["finish_reason"] == "length":
            reason = "output_truncated"
        elif message.invalid_tool_calls:
            reason = "invalid_tool_arguments"
        else:
            reason = "missing_submission"
        observation["failure"] = reason
        retry = self._repair(reason)
        return {"submission_control": self.control, "jump_to": "model" if retry else "end"}

    def wrap_model_call(self, request, handler):
        self.hydrate(request.state)
        if not self.control["force_submit"]:
            return handler(request)
        tools = [
            t
            for t in request.tools
            if (t.name if hasattr(t, "name") else t.get("name")) == "submit_review"
        ]
        blocks = list(request.system_message.content_blocks) if request.system_message else []
        blocks.append(
            {
                "type": "text",
                "text": (
                    "Final submission correction is required. The previous response did not "
                    "produce "
                    "an accepted review. Call submit_review with valid JSON matching its schema. "
                    "Use only findings supported by the existing code and tool evidence. Correct "
                    "any reported location/evidence errors. If no finding is defensible, "
                    "explicitly "
                    "submit findings=[]. Do not write a prose-only answer or investigate further. "
                    "Keep explanations concise to fit the output limit."
                ),
            }
        )
        return handler(
            request.override(
                tools=tools,
                tool_choice="submit_review",
                system_message=SystemMessage(content=blocks),
            )
        )

    def manifest(self):
        return {
            **self.control,
            "repair_limit": self.policy.submission_repairs,
            "outcome": "submitted" if self.session.findings is not None else "not_submitted",
        }
