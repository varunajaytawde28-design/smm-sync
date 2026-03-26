"""Tests for temporal contradiction detection and confidence scoring.

Research basis: EVOKG (MIT CSAIL + IBM Research, Sep 2025) — 23.3% improvement
in temporal graph accuracy when superseding relationships are tracked.
Source confidence hierarchy: manual > github_pr > github_release > meeting >
github_issue > slack > github_commit.

All Graphiti/Anthropic API calls are mocked — no real network calls.
"""
from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from smm_sync.context_graph.client import SOURCE_CONFIDENCE, GraphClient
from smm_sync.context_graph.models import ContextResult


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _run(coro):
    return asyncio.run(coro)


@pytest.fixture()
def graph_dir(tmp_path) -> Path:
    return tmp_path / "graph"


@pytest.fixture()
def client(graph_dir) -> GraphClient:
    return GraphClient(graph_dir=graph_dir, api_key="fake-api-key")


# ---------------------------------------------------------------------------
# SOURCE_CONFIDENCE hierarchy
# ---------------------------------------------------------------------------

class TestSourceConfidence:
    def test_manual_is_highest(self):
        assert SOURCE_CONFIDENCE["manual"] == 0.95

    def test_github_pr_high_confidence(self):
        assert SOURCE_CONFIDENCE["github_pr"] == 0.90

    def test_github_release_high_confidence(self):
        assert SOURCE_CONFIDENCE["github_release"] == 0.88

    def test_github_commit_lowest(self):
        assert SOURCE_CONFIDENCE["github_commit"] == 0.60

    def test_github_issue_below_meeting(self):
        assert SOURCE_CONFIDENCE["github_issue"] < SOURCE_CONFIDENCE["meeting"]

    def test_all_values_in_range(self):
        for key, val in SOURCE_CONFIDENCE.items():
            assert 0.0 <= val <= 1.0, f"{key} confidence {val} out of range"


# ---------------------------------------------------------------------------
# _calculate_confidence
# ---------------------------------------------------------------------------

class TestCalculateConfidence:
    @pytest.mark.asyncio
    async def test_base_score_from_source_type(self, client):
        score = await client._calculate_confidence(
            source_type="github_pr",
            content="short",
            has_alternatives=False,
            has_rationale=False,
        )
        assert score == SOURCE_CONFIDENCE["github_pr"]

    @pytest.mark.asyncio
    async def test_boosted_for_alternatives(self, client):
        base = SOURCE_CONFIDENCE["github_pr"]
        score = await client._calculate_confidence(
            source_type="github_pr",
            content="short",
            has_alternatives=True,
            has_rationale=False,
        )
        assert score > base

    @pytest.mark.asyncio
    async def test_boosted_for_long_rationale(self, client):
        base = SOURCE_CONFIDENCE["github_pr"]
        score = await client._calculate_confidence(
            source_type="github_pr",
            content="x" * 300,  # > 200 chars
            has_alternatives=False,
            has_rationale=True,
        )
        assert score > base

    @pytest.mark.asyncio
    async def test_boosted_for_comprehensive_content(self, client):
        base = SOURCE_CONFIDENCE["github_pr"]
        score = await client._calculate_confidence(
            source_type="github_pr",
            content="x" * 600,  # > 500 chars
            has_alternatives=False,
            has_rationale=False,
        )
        assert score > base

    @pytest.mark.asyncio
    async def test_capped_at_1_0(self, client):
        # manual (0.95) + all three boosts (0.15) would exceed 1.0
        score = await client._calculate_confidence(
            source_type="manual",
            content="x" * 600,
            has_alternatives=True,
            has_rationale=True,
        )
        assert score <= 1.0

    @pytest.mark.asyncio
    async def test_unknown_source_type_defaults_to_0_5(self, client):
        score = await client._calculate_confidence(
            source_type="unknown_source",
            content="short",
            has_alternatives=False,
            has_rationale=False,
        )
        assert score == 0.50

    @pytest.mark.asyncio
    async def test_github_commit_gets_lower_base(self, client):
        commit_score = await client._calculate_confidence(
            source_type="github_commit",
            content="short",
            has_alternatives=False,
            has_rationale=False,
        )
        pr_score = await client._calculate_confidence(
            source_type="github_pr",
            content="short",
            has_alternatives=False,
            has_rationale=False,
        )
        assert commit_score < pr_score


# ---------------------------------------------------------------------------
# contradiction_check
# ---------------------------------------------------------------------------

class TestContradictionCheck:
    @pytest.mark.asyncio
    async def test_returns_empty_for_unrelated_content(self, client):
        """No contradictions when search returns empty."""
        with patch.object(client, "search_context", new=AsyncMock(return_value=[])):
            result = await client.contradiction_check("Some new decision", "test-project")

        assert result == []

    @pytest.mark.asyncio
    async def test_returns_contradiction_for_high_similarity(self, client):
        """Returns contradiction when existing decision has relevance_score > 0.75."""
        existing = ContextResult(
            title="Use os.rename() for locking",
            content="We use os.rename() for atomic file locking.",
            relevance_score=0.85,
            excerpt="We use os.rename() for atomic file locking.",
        )
        with patch.object(client, "search_context", new=AsyncMock(return_value=[existing])):
            result = await client.contradiction_check(
                "Switch to Redis for file locking", "test-project"
            )

        assert len(result) == 1
        assert result[0]["existing"] == "Use os.rename() for locking"
        assert result[0]["action"] == "superseded_by"
        assert result[0]["similarity"] == 0.85

    @pytest.mark.asyncio
    async def test_no_contradiction_for_low_similarity(self, client):
        """No contradiction when relevance_score <= 0.75."""
        existing = ContextResult(
            title="Unrelated decision",
            content="Something about UI colors.",
            relevance_score=0.50,
            excerpt="Something about UI colors.",
        )
        with patch.object(client, "search_context", new=AsyncMock(return_value=[existing])):
            result = await client.contradiction_check("New locking strategy", "test-project")

        assert result == []

    @pytest.mark.asyncio
    async def test_never_raises_on_search_failure(self, client):
        """contradiction_check must return [] and never raise on failure."""
        with patch.object(client, "search_context", new=AsyncMock(side_effect=RuntimeError("DB gone"))):
            result = await client.contradiction_check("anything", "project")

        assert result == []

    @pytest.mark.asyncio
    async def test_returns_multiple_contradictions(self, client):
        """Can detect multiple contradictions at once."""
        results = [
            ContextResult(
                title="Decision A", content="A", relevance_score=0.90, excerpt="A"
            ),
            ContextResult(
                title="Decision B", content="B", relevance_score=0.82, excerpt="B"
            ),
            ContextResult(
                title="Decision C", content="C", relevance_score=0.60, excerpt="C"
            ),
        ]
        with patch.object(client, "search_context", new=AsyncMock(return_value=results)):
            contradictions = await client.contradiction_check("new decision", "project")

        # Only A and B have score > 0.75
        assert len(contradictions) == 2
        titles = [c["existing"] for c in contradictions]
        assert "Decision A" in titles
        assert "Decision B" in titles
        assert "Decision C" not in titles


# ---------------------------------------------------------------------------
# add_decision with contradiction detection
# ---------------------------------------------------------------------------

class TestAddDecisionWithContradiction:
    @pytest.mark.asyncio
    async def test_add_decision_still_writes_when_contradiction_found(self, client):
        """Contradiction must never block add_decision — always write."""
        mock_graphiti = MagicMock()
        mock_episode = MagicMock()
        mock_episode.episode.uuid = "test-uuid-123"
        mock_graphiti.add_episode = AsyncMock(return_value=mock_episode)
        client._graphiti = mock_graphiti
        client._driver = MagicMock()

        contradiction = ContextResult(
            title="Old decision",
            content="Old approach.",
            relevance_score=0.90,
            excerpt="Old approach.",
        )
        with patch.object(client, "search_context", new=AsyncMock(return_value=[contradiction])):
            result = await client.add_decision(
                title="New decision",
                content="New approach replaces old.",
                rationale="Better performance.",
                made_by="test",
                project="test-project",
            )

        # Write happened despite contradiction
        assert mock_graphiti.add_episode.called
        assert result == "test-uuid-123"

    @pytest.mark.asyncio
    async def test_contradiction_metadata_added_to_episode_body(self, client):
        """Contradiction titles must appear in the episode body."""
        captured_body = {}
        mock_graphiti = MagicMock()

        async def fake_add_episode(name, episode_body, **kwargs):
            captured_body["body"] = episode_body
            result = MagicMock()
            result.episode.uuid = "uuid-abc"
            return result

        mock_graphiti.add_episode = fake_add_episode
        client._graphiti = mock_graphiti
        client._driver = MagicMock()

        contradiction = ContextResult(
            title="Contradicted Decision XYZ",
            content="The old way.",
            relevance_score=0.88,
            excerpt="The old way.",
        )
        with patch.object(client, "search_context", new=AsyncMock(return_value=[contradiction])):
            await client.add_decision(
                title="New decision",
                content="New way replaces old.",
                rationale="Simpler.",
                made_by="test",
                project="project",
            )

        assert "Contradictions detected:" in captured_body["body"]
        assert "Contradicted Decision XYZ" in captured_body["body"]

    @pytest.mark.asyncio
    async def test_add_decision_writes_without_contradiction(self, client):
        """Normal add_decision with no contradictions writes cleanly."""
        mock_graphiti = MagicMock()
        mock_episode = MagicMock()
        mock_episode.episode.uuid = "clean-uuid"
        mock_graphiti.add_episode = AsyncMock(return_value=mock_episode)
        client._graphiti = mock_graphiti
        client._driver = MagicMock()

        with patch.object(client, "search_context", new=AsyncMock(return_value=[])):
            result = await client.add_decision(
                title="Clean decision",
                content="No contradictions here.",
                rationale="First of its kind.",
                made_by="test",
                project="project",
            )

        assert result == "clean-uuid"
        assert mock_graphiti.add_episode.called
