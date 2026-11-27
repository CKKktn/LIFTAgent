"""JSON-only outputs used by the collaborative-review LangChain nodes.

Compatible chat endpoints need not implement provider-specific schema mode.
Validation is local, strict, bounded and never stores model credentials.
"""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field
from .review_evidence import SOURCES_PER_SEARCH

MAX_COLLAB_SOURCES = 6 * SOURCES_PER_SEARCH


class CollabOutput(BaseModel):
    model_config = ConfigDict(extra="ignore", strict=True)


class RewriteOutput(CollabOutput):
    standalone_question: str = Field(min_length=1, max_length=16000)
    intent: Literal["question", "review"]
    # None permits legacy compatible providers/fixtures that omit this field.
    context_dependent: bool | None = None


class EvidenceOutput(CollabOutput):
    sufficient: bool
    relevant_ids: list[int] = Field(default_factory=list, max_length=MAX_COLLAB_SOURCES)
    missing: list[str] = Field(default_factory=list, max_length=20)
    conflicts: list[str] = Field(default_factory=list, max_length=20)
    summary: str = Field(min_length=1, max_length=4000)


class ValidationOutput(CollabOutput):
    grounded: bool
    issues: list[str] = Field(default_factory=list, max_length=30)
    supported_ids: list[int] = Field(default_factory=list, max_length=MAX_COLLAB_SOURCES)
    summary: str = Field(min_length=1, max_length=4000)
    verdict: Literal["已满足", "不满足", "待核实", "无审查判定"] | None = None
