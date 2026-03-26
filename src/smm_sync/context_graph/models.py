"""Pydantic models for context graph results."""
from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field


class Decision(BaseModel):
    """A recorded team or architectural decision.

    Args:
        id: Unique identifier (UUID string).
        title: Short title of the decision.
        content: Full description of what was decided.
        rationale: Why this decision was made.
        constraints: Known constraints imposed by this decision.
        alternatives: Alternatives that were considered and rejected.
        made_by: Who made this decision.
        project: Project name (graph partition).
        source: Source description (e.g. "decision by Varun").
        created_at: When the decision was recorded.
        valid: False if this decision has been superseded or invalidated.
    """

    id: str
    title: str
    content: str
    rationale: str
    constraints: list[str] = Field(default_factory=list)
    alternatives: list[str] = Field(default_factory=list)
    made_by: str
    project: str
    source: str = ""
    created_at: datetime = Field(default_factory=datetime.utcnow)
    valid: bool = True


class ContextResult(BaseModel):
    """A single search result from the context graph.

    Args:
        title: Title or relation name of the matched fact.
        content: Full fact text returned by the graph.
        relevance_score: Similarity score (0.0–1.0 range, higher = more relevant).
        excerpt: Most relevant 2-3 sentences from content.
    """

    title: str
    content: str
    relevance_score: float = 0.0
    excerpt: str = ""


class RejectionResult(BaseModel):
    """A previously-rejected alternative surfaced by the déjà vu check.

    Args:
        rejected_alternative: The alternative approach that was rejected.
        decision_title: Title of the decision that rejected this alternative.
        rationale: Why the alternative was rejected.
        decided_at: ISO timestamp when the decision was made.
        confidence: Confidence score of the source decision (0.0–1.0).
        decision_id: UUID of the source decision.
    """

    rejected_alternative: str
    decision_title: str
    rationale: str
    decided_at: str
    confidence: float
    decision_id: str
