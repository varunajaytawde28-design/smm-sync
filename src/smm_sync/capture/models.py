"""Pydantic models for GitHub capture config and events."""
from __future__ import annotations

from datetime import datetime
from typing import Optional

from pydantic import BaseModel, Field


class CaptureTypes(BaseModel):
    """Which event types to capture for a repo.

    Args:
        pull_requests: Capture PR titles, descriptions, and comments.
        commits: Capture commit messages.
        issues: Capture issue titles and descriptions.
        releases: Capture release notes.
    """

    pull_requests: bool = True
    commits: bool = True
    issues: bool = True
    releases: bool = True


class RepoConfig(BaseModel):
    """Configuration for a single GitHub repository.

    Args:
        owner: GitHub username or org.
        name: Repository name.
        project: Maps to graph project name.
        capture: Which event types to capture.
    """

    owner: str
    name: str
    project: str
    capture: CaptureTypes = Field(default_factory=CaptureTypes)

    @property
    def full_name(self) -> str:
        """Return 'owner/name' string."""
        return f"{self.owner}/{self.name}"


class CaptureSettings(BaseModel):
    """Global capture settings.

    Args:
        poll_interval_minutes: How often to check for new events.
        lookback_days: How far back to fetch on first run.
        min_content_length: Skip content shorter than this.
        decision_keywords: Keywords that signal a decision worth extracting.
        pr_context_injection: Post CaaS context comments on new PRs (ProAIDE
            workflow boundary injection — 52% engagement vs 62% dismissal
            mid-task). Default True.
    """

    poll_interval_minutes: int = 30
    lookback_days: int = 30
    min_content_length: int = 50
    pr_context_injection: bool = True
    decision_keywords: list[str] = Field(
        default_factory=lambda: [
            "decided",
            "chose",
            "rejected",
            "because",
            "instead of",
            "rationale",
            "trade-off",
            "constraint",
            "we will",
            "we won't",
        ]
    )


class GithubCaptureConfig(BaseModel):
    """Top-level config loaded from .smm/github.yml.

    Args:
        repos: List of repos to watch.
        settings: Global polling and filtering settings.
    """

    repos: list[RepoConfig]
    settings: CaptureSettings = Field(default_factory=CaptureSettings)


class CapturedEvent(BaseModel):
    """A single GitHub event captured and written to the graph.

    Args:
        repo: 'owner/name' string.
        event_type: One of 'pr', 'commit', 'issue', 'release'.
        event_id: PR number, commit SHA, issue number, or release ID.
        title: Short title of the event.
        content: Full text content.
        url: GitHub URL to the event.
        captured_at: When we captured this event.
        decision_extracted: The extracted decision sentence, or None.
    """

    repo: str
    event_type: str
    event_id: str
    title: str
    content: str
    url: str
    captured_at: datetime = Field(default_factory=datetime.utcnow)
    decision_extracted: Optional[str] = None
