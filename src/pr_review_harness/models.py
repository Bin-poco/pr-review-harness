"""Shared contracts for versioned review inputs and outputs."""

from dataclasses import dataclass, field
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


@dataclass(frozen=True)
class ChangedFile:
    path: str
    status: str
    patch: str
    added_ranges: tuple[tuple[int, int], ...]


@dataclass(frozen=True)
class ContextItem:
    path: str
    reason: str
    content: str


@dataclass(frozen=True)
class ContextPack:
    text: str
    items: tuple[ContextItem, ...]
    omitted: tuple[str, ...]
    max_chars: int


class Finding(BaseModel):
    """A proposed new defect, anchored to the immutable head commit."""

    model_config = ConfigDict(extra="forbid")
    path: str
    line: int = Field(ge=1)
    severity: Literal["P1", "P2", "P3"]
    title: str = Field(min_length=1, max_length=160)
    explanation: str = Field(min_length=1, max_length=4000)
    trigger: str = Field(min_length=1, max_length=2000)
    evidence_ids: list[str] = Field(default_factory=list)
    confidence: Literal["high", "medium", "low"] = "medium"


@dataclass(frozen=True)
class CheckRun:
    version: str
    sha: str
    status: str
    exit_code: int | None
    output: str
    cache: dict | None = None


@dataclass(frozen=True)
class Evidence:
    id: str
    kind: str
    path: str
    base: CheckRun
    head: CheckRun
    same_check: bool | None = None


@dataclass
class ReviewSession:
    sequence: int = 0
    evidence: list[Evidence] = field(default_factory=list)
    findings: list[Finding] | None = None
    rejected: list[str] = field(default_factory=list)
    tool_calls: int = 0
    read_chars: int = 0
    trace: list[dict] = field(default_factory=list)
