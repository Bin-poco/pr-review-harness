"""Checkpointed review facts; the mutable session is only an execution view."""

from dataclasses import asdict
from typing import Annotated

from deepagents import DeepAgentState

from pr_review_harness.models import CheckRun, Evidence, Finding


def latest(left: dict, right: dict) -> dict:
    """Tool batches merge cumulative, serially produced state by sequence."""
    return right if right.get("sequence", 0) >= left.get("sequence", 0) else left


class ReviewState(DeepAgentState):
    review_session: Annotated[dict, latest]
    run_manifest: dict
    memory_snapshot: dict
    context_manifest: dict
    submission_control: dict


def dump_session(session):
    return {
        "sequence": session.sequence,
        "evidence": [asdict(e) for e in session.evidence],
        "findings": None
        if session.findings is None
        else [f.model_dump() for f in session.findings],
        "rejected": list(session.rejected),
        "tool_calls": session.tool_calls,
        "read_chars": session.read_chars,
        "trace": list(session.trace),
    }


def load_session(session, value):
    session.sequence = value.get("sequence", 0)
    session.evidence = [
        Evidence(**{**e, "base": CheckRun(**e["base"]), "head": CheckRun(**e["head"])})
        for e in value.get("evidence", [])
    ]
    findings = value.get("findings")
    session.findings = None if findings is None else [Finding(**f) for f in findings]
    session.rejected = list(value.get("rejected", []))
    session.tool_calls = value.get("tool_calls", 0)
    session.read_chars = value.get("read_chars", 0)
    session.trace = list(value.get("trace", []))
