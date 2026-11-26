"""Local output schemas for compatible chat providers without schema-mode support."""
from __future__ import annotations

from typing import Any, Literal
from pydantic import BaseModel, ConfigDict, Field

RequirementType = Literal["document", "numeric", "functional"]
Verdict = Literal["已满足", "部分满足", "不满足", "证据不足", "待人工复核", "证据冲突"]


class ExtractionOutput(BaseModel):
    model_config = ConfigDict(extra="ignore")
    # Validate candidates separately so one bad reference never discards good rows.
    technical_indicators: list[Any]
    closure_conditions: list[Any]


class VisualExtractionOutput(ExtractionOutput):
    page_text: str = Field(max_length=50000)


class ExtractedRequirement(BaseModel):
    model_config = ConfigDict(extra="ignore")
    type: RequirementType
    subject: str = Field(min_length=1, max_length=500)
    title: str = Field(min_length=1, max_length=500)
    criterion: str = Field(min_length=1, max_length=12000)
    rationale: str = Field(min_length=1, max_length=2000)
    document_name: str = Field(default="", max_length=500)
    source_ids: list[str] = Field(default_factory=list, max_length=100)
    quote: str = Field(default="", max_length=16000)


class JudgmentOutput(BaseModel):
    model_config = ConfigDict(extra="ignore")
    status: Verdict
    project_plan: str = Field(default="模型未提供项目方案或实现情况说明", max_length=20000)
    analysis: str = Field(min_length=1, max_length=30000)
    gaps: str = Field(default="模型未提供缺口说明", max_length=20000)
    recommendation: str = Field(default="模型未提供补证建议", max_length=20000)
    evidence: list[Any] = Field(max_length=100)


class DocumentMatches(BaseModel):
    model_config = ConfigDict(extra="ignore")
    matches: list[Any] = Field(max_length=100)


class PolishedOutput(BaseModel):
    model_config = ConfigDict(extra="ignore")
    sections: list[Any] = Field(max_length=1000)
