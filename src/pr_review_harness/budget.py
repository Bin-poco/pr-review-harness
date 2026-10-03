"""One policy for complete model requests and bounded business resources."""

import json
import math
from dataclasses import asdict, dataclass

from deepagents.middleware.summarization import ContextOverflowError
from langchain_core.messages import HumanMessage
from langchain_core.utils.function_calling import convert_to_openai_tool


class BudgetExceeded(ContextOverflowError):
    """An irreducible request or cumulative run cannot fit the configured policy."""


@dataclass(frozen=True)
class BudgetPolicy:
    context_chars: int = 24000
    memory_chars: int = 4000
    working_chars: int = 6000
    read_chars: int = 40000
    verify_read_chars: int = 20000
    request_chars: int = 120000
    model_calls: int = 12
    tool_calls: int = 24
    verify_model_calls: int = 24
    verify_tool_calls: int = 32
    total_model_calls: int = 64
    total_tool_calls: int = 100
    window_tokens: int | None = None
    output_tokens: int = 4096
    window_source: str = "unknown; bounded character mode"
    working_state: bool = True
    submission_repairs: int = 2

    def __post_init__(self):
        for key, value in asdict(self).items():
            if key in {"window_source", "working_state", "window_tokens", "submission_repairs"}:
                continue
            if type(value) is not int or value < 1:
                raise ValueError(f"{key} must be a positive integer")
        if type(self.submission_repairs) is not int or not 0 <= self.submission_repairs <= 5:
            raise ValueError("submission_repairs must be an integer between 0 and 5")
        if self.window_tokens is not None:
            if type(self.window_tokens) is not int or self.input_limit < 512:
                raise ValueError("Model window is too small for output and safety reserve")
        if self.total_model_calls < self.model_calls or self.total_tool_calls < self.tool_calls:
            raise ValueError("Global call limits must cover the review stage limits")

    @property
    def input_limit(self):
        if self.window_tokens is None:
            return self.request_chars
        return (
            self.window_tokens - self.output_tokens - max(2048, math.ceil(self.window_tokens * 0.1))
        )

    def manifest(self):
        return {
            **asdict(self),
            "input_limit": self.input_limit,
            "mode": "token-estimate" if self.window_tokens else "characters",
        }

    @classmethod
    def for_model(cls, model, **kwargs):
        if kwargs.get("window_tokens") is not None:
            kwargs.setdefault("window_source", "explicit configuration")
        else:
            profile = getattr(model, "profile", None)
            window = profile.get("max_input_tokens") if isinstance(profile, dict) else None
            if type(window) is int and window > 0:
                kwargs["window_tokens"] = window
                kwargs.setdefault("window_source", "model.profile.max_input_tokens")
        return cls(**kwargs)


class RequestCounter:
    """Use the adapter counter when available, otherwise a conservative byte estimate.

    Adapter counts remain estimates of provider serialization. Unknown windows use
    character accounting without claiming a token guarantee. Tool schemas are counted.
    """

    def __init__(self, model, policy):
        self.model = model
        self.policy = policy
        self.method = "characters" if policy.window_tokens is None else "utf8-byte-estimate"

    def __call__(self, messages, *, tools=None):
        schemas = [convert_to_openai_tool(t) for t in (tools or [])]
        if self.policy.window_tokens is not None:
            # Generic BaseChatModel implementations may ignore tools; only use the
            # adapter that actually implements schema-aware counting in this project.
            from langchain_openai import ChatOpenAI

            if isinstance(self.model, ChatOpenAI):
                try:
                    count = self.model.get_num_tokens_from_messages(messages)
                    # Pinned adapter ignores its tools parameter; count serialized
                    # schemas explicitly and reserve additional protocol overhead.
                    if schemas:
                        count += self.model.get_num_tokens(json.dumps(schemas, ensure_ascii=False))
                        count += 64 * len(schemas)
                    count += 32
                    self.method = "langchain-openai adapter estimate"
                    return count
                except (NotImplementedError, KeyError, ValueError):
                    pass
        payload = json.dumps(
            {"messages": [m.model_dump(mode="json") for m in messages], "tools": schemas},
            ensure_ascii=False,
            separators=(",", ":"),
        )
        if self.policy.window_tokens is None:
            return len(payload)
        return len(payload.encode("utf-8")) + 32 * len(messages) + 64 * len(schemas)

    def text(self, value):
        return self([HumanMessage(content=value)])

    def fit_text(self, value, limit):
        """Return a prefix with an explicit omission marker, preserving the original elsewhere."""
        return self.fit_prefix(value, limit)[0]

    def fit_prefix(self, value, limit):
        """Return rendered text and the source-prefix length, excluding our marker."""
        if self.text(value) <= limit:
            return value, len(value)
        marker = "\n[OMITTED by unified budget; use fixed-version tools for original.]"
        if self.text(marker) > limit:
            return "", 0
        low, high = 0, len(value)
        while low < high:
            mid = (low + high + 1) // 2
            if self.text(value[:mid] + marker) <= limit:
                low = mid
            else:
                high = mid - 1
        return value[:low] + marker, low


DEFAULT_POLICY = BudgetPolicy()
