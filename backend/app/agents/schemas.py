"""Structured outputs every agent must return. Validated with Pydantic; free text is never parsed for decisions."""

from typing import Literal

from pydantic import BaseModel, Field

Sev = Literal["info", "low", "medium", "high", "critical"]


class FileRef(BaseModel):
    file: str = Field(description="Repository-relative path")
    line: int = Field(ge=1, description="1-based line number")
    explanation: str = ""


class Triage(BaseModel):
    """Bug Hunter: is an automatically detected finding a real defect worth fixing?"""
    is_real_bug: bool
    confidence: float = Field(ge=0, le=1)
    severity: Sev
    rationale: str


class CodeAnalysis(BaseModel):
    """Code Analyst: what code is involved and how it connects."""
    summary: str
    relevant_files: list[FileRef] = Field(default_factory=list, max_length=30)
    key_symbols: list[str] = Field(default_factory=list, max_length=40)
    dependencies: str = ""


class Hypothesis(BaseModel):
    claim: str
    evidence: list[FileRef] = Field(min_length=1, max_length=20)
    confidence: float = Field(ge=0, le=1)
    reproduced: bool = Field(default=False, description="True only if a command actually demonstrated the failure")
    reproduction: str = Field(default="", description="Command used and its observed output")


class Investigation(BaseModel):
    """Investigation agent: ranked root-cause hypotheses, each backed by file:line evidence."""
    hypotheses: list[Hypothesis] = Field(min_length=1, max_length=5)


class FixResult(BaseModel):
    summary: str
    rationale: str
    files_changed: list[str] = Field(default_factory=list)


class TestingResult(BaseModel):
    __test__ = False  # not a pytest class
    summary: str
    test_files: list[str] = Field(default_factory=list)
    test_names: list[str] = Field(default_factory=list)


class Issue(BaseModel):
    severity: Sev
    file: str = ""
    line: int | None = None
    description: str


class SecurityReview(BaseModel):
    verdict: Literal["pass", "block"]
    issues: list[Issue] = Field(default_factory=list)
    summary: str = ""


class ReviewVerdict(BaseModel):
    verdict: Literal["approve", "changes_requested", "reject"]
    summary: str
    issues: list[Issue] = Field(default_factory=list)
