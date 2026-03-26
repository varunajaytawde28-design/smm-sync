"""Tests for PR context injection (workflow boundary surfacing).

Research basis: ProAIDE (JetBrains 2026) — 52% engagement at workflow
boundaries vs 62% dismissal mid-task, p=0.0016. PR creation is the
optimal cognitive load state for receiving architectural context.

METR RCT: developers are 19% slower with AI when AI violates implicit
constraints. PR injection is the direct antidote.

All GitHub API and Anthropic calls are mocked — no real network calls.
"""
from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import yaml

from smm_sync.context_graph.models import ContextResult


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _run(coro):
    return asyncio.run(coro)


@pytest.fixture()
def valid_github_yml(tmp_path) -> Path:
    config = {
        "repos": [
            {
                "owner": "testorg",
                "name": "myrepo",
                "project": "test-project",
                "capture": {
                    "pull_requests": True,
                    "commits": False,
                    "issues": False,
                    "releases": False,
                },
            }
        ],
        "settings": {
            "poll_interval_minutes": 30,
            "lookback_days": 7,
            "min_content_length": 10,
            "decision_keywords": ["decided", "chose", "because"],
            "pr_context_injection": True,
        },
    }
    path = tmp_path / "github.yml"
    path.write_text(yaml.dump(config), encoding="utf-8")
    return path


@pytest.fixture()
def mock_graph_client():
    client = MagicMock()
    client.add_decision = AsyncMock(return_value="fake-uuid")
    client.search_context = AsyncMock(return_value=[])
    return client


@pytest.fixture()
def capture(valid_github_yml, mock_graph_client, tmp_path):
    from smm_sync.capture.github_capture import GitHubCapture

    return GitHubCapture(
        config_path=valid_github_yml,
        state_path=tmp_path / "capture_state.json",
        graph_client=mock_graph_client,
        github_token="fake-token",
        api_key="fake-api-key",
    )


@pytest.fixture()
def repo_config(capture):
    return capture.config.repos[0]


# ---------------------------------------------------------------------------
# inject_pr_context: posts comment when constraints found
# ---------------------------------------------------------------------------

class TestInjectPrContextPostsComment:
    @pytest.mark.asyncio
    async def test_posts_comment_when_constraints_found(self, capture, repo_config, mock_graph_client):
        """inject_pr_context must post a comment when relevant constraints found."""
        mock_graph_client.search_context = AsyncMock(return_value=[
            ContextResult(
                title="Never expose raw MCP",
                content="Do not expose raw MCP to enterprise customers.",
                relevance_score=0.90,
                excerpt="Do not expose raw MCP to enterprise customers.",
            )
        ])

        mock_pr = MagicMock()
        mock_repo = MagicMock()
        mock_repo.get_pull.return_value = mock_pr

        with patch.object(capture, "_get_github") as mock_gh:
            mock_gh.return_value.get_repo.return_value = mock_repo
            result = await capture.inject_pr_context(
                repo_config=repo_config,
                pr_number=42,
                pr_title="Add MCP endpoint",
                pr_body="This adds a new MCP endpoint for enterprise use.",
                changed_files=["src/mcp_server.py"],
            )

        assert result is True
        mock_pr.create_issue_comment.assert_called_once()
        comment_text = mock_pr.create_issue_comment.call_args[0][0]
        assert "CaaS Context" in comment_text
        assert "Never expose raw MCP" in comment_text

    @pytest.mark.asyncio
    async def test_comment_format_is_correct_markdown(self, capture, repo_config, mock_graph_client):
        """PR comment must be valid markdown with correct structure."""
        mock_graph_client.search_context = AsyncMock(return_value=[
            ContextResult(
                title="Use atomic writes",
                content="Always use os.rename() for atomic writes.",
                relevance_score=0.85,
                excerpt="Always use os.rename() for atomic writes.",
            )
        ])

        mock_pr = MagicMock()
        mock_repo = MagicMock()
        mock_repo.get_pull.return_value = mock_pr

        with patch.object(capture, "_get_github") as mock_gh:
            mock_gh.return_value.get_repo.return_value = mock_repo
            await capture.inject_pr_context(
                repo_config=repo_config,
                pr_number=10,
                pr_title="Refactor file writes",
                pr_body="Changes file write pattern.",
                changed_files=["src/state.py"],
            )

        comment = mock_pr.create_issue_comment.call_args[0][0]
        # Must have markdown heading
        assert "##" in comment
        # Must mention CaaS
        assert "CaaS" in comment
        # Must have numbered constraint
        assert "**1." in comment
        # Must have blockquote
        assert ">" in comment
        # Must have footer separator
        assert "---" in comment


# ---------------------------------------------------------------------------
# inject_pr_context: skips when no relevant constraints
# ---------------------------------------------------------------------------

class TestInjectPrContextSkips:
    @pytest.mark.asyncio
    async def test_skips_when_no_relevant_constraints(self, capture, repo_config, mock_graph_client):
        """inject_pr_context must return False and skip when no results found."""
        mock_graph_client.search_context = AsyncMock(return_value=[])

        mock_pr = MagicMock()
        with patch.object(capture, "_get_github") as mock_gh:
            mock_gh.return_value.get_repo.return_value.get_pull.return_value = mock_pr
            result = await capture.inject_pr_context(
                repo_config=repo_config,
                pr_number=5,
                pr_title="Fix typo",
                pr_body="Changed 'teh' to 'the'.",
                changed_files=["README.md"],
            )

        assert result is False
        mock_pr.create_issue_comment.assert_not_called()

    @pytest.mark.asyncio
    async def test_query_includes_pr_title_and_files(self, capture, repo_config, mock_graph_client):
        """The search query must include PR title, body, and changed files."""
        captured_query = {}

        async def capture_search(query, project, limit):
            captured_query["query"] = query
            return []

        mock_graph_client.search_context = capture_search

        with patch.object(capture, "_get_github"):
            await capture.inject_pr_context(
                repo_config=repo_config,
                pr_number=7,
                pr_title="Refactor coordinator",
                pr_body="Updates file claiming logic.",
                changed_files=["src/coordinator.py", "tests/test_coordinator.py"],
            )

        assert "Refactor coordinator" in captured_query["query"]
        assert "coordinator.py" in captured_query["query"]


# ---------------------------------------------------------------------------
# inject_pr_context: handles GitHub API errors gracefully
# ---------------------------------------------------------------------------

class TestInjectPrContextErrorHandling:
    @pytest.mark.asyncio
    async def test_returns_false_on_github_api_error(self, capture, repo_config, mock_graph_client):
        """inject_pr_context must return False (not raise) when GitHub API fails."""
        mock_graph_client.search_context = AsyncMock(return_value=[
            ContextResult(
                title="Some constraint",
                content="Important constraint.",
                relevance_score=0.90,
                excerpt="Important constraint.",
            )
        ])

        with patch.object(capture, "_get_github") as mock_gh:
            mock_gh.return_value.get_repo.side_effect = Exception("GitHub API down")
            result = await capture.inject_pr_context(
                repo_config=repo_config,
                pr_number=99,
                pr_title="Something",
                pr_body="Body.",
                changed_files=[],
            )

        assert result is False

    @pytest.mark.asyncio
    async def test_returns_false_on_search_failure(self, capture, repo_config, mock_graph_client):
        """inject_pr_context must return False when graph search fails."""
        mock_graph_client.search_context = AsyncMock(
            side_effect=RuntimeError("Graph DB unavailable")
        )

        result = await capture.inject_pr_context(
            repo_config=repo_config,
            pr_number=1,
            pr_title="Some PR",
            pr_body="Some body.",
            changed_files=[],
        )

        assert result is False

    @pytest.mark.asyncio
    async def test_handles_create_comment_failure_gracefully(self, capture, repo_config, mock_graph_client):
        """inject_pr_context must return False (not raise) when create_issue_comment fails."""
        mock_graph_client.search_context = AsyncMock(return_value=[
            ContextResult(
                title="Constraint A",
                content="Constraint body.",
                relevance_score=0.88,
                excerpt="Constraint body.",
            )
        ])

        mock_pr = MagicMock()
        mock_pr.create_issue_comment.side_effect = Exception("Comment creation failed")
        mock_repo = MagicMock()
        mock_repo.get_pull.return_value = mock_pr

        with patch.object(capture, "_get_github") as mock_gh:
            mock_gh.return_value.get_repo.return_value = mock_repo
            result = await capture.inject_pr_context(
                repo_config=repo_config,
                pr_number=3,
                pr_title="Some PR",
                pr_body="Body.",
                changed_files=["src/file.py"],
            )

        assert result is False
