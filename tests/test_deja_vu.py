"""Tests for the Déjà Vu rejection detection feature."""
from __future__ import annotations

import pytest
from unittest.mock import AsyncMock, MagicMock


# ---------------------------------------------------------------------------
# check_rejected_alternatives (GraphClient)
# ---------------------------------------------------------------------------

class TestCheckRejectedAlternatives:
    """Tests for GraphClient.check_rejected_alternatives."""

    @pytest.mark.asyncio
    async def test_returns_empty_on_no_matches(self):
        """Returns empty list when no rejection keywords found in results."""
        from smm_sync.context_graph.client import GraphClient
        from smm_sync.context_graph.models import ContextResult

        client = MagicMock(spec=GraphClient)
        # Patch search_context to return results without rejection keywords
        client.search_context = AsyncMock(return_value=[
            ContextResult(
                title="Use Kuzu",
                content="We chose Kuzu as the embedded graph database.",
                relevance_score=0.85,
                excerpt="We chose Kuzu.",
            )
        ])
        client.check_rejected_alternatives = GraphClient.check_rejected_alternatives.__get__(
            client, GraphClient
        )

        result = await client.check_rejected_alternatives(query="graph database", project="test")
        assert isinstance(result, list)
        assert result == []

    @pytest.mark.asyncio
    async def test_returns_result_on_rejection_keyword(self):
        """Returns RejectionResult when content contains rejection keywords."""
        from smm_sync.context_graph.client import GraphClient
        from smm_sync.context_graph.models import ContextResult, RejectionResult

        client = MagicMock(spec=GraphClient)
        client.search_context = AsyncMock(return_value=[
            ContextResult(
                title="Graph DB choice",
                content="We considered Neo4j but rejected it due to licensing costs. Chose Kuzu instead.",
                relevance_score=0.90,
                excerpt="We considered Neo4j but rejected it.",
            )
        ])
        client.check_rejected_alternatives = GraphClient.check_rejected_alternatives.__get__(
            client, GraphClient
        )

        result = await client.check_rejected_alternatives(query="Neo4j graph database", project="test")
        assert isinstance(result, list)
        assert len(result) >= 1
        assert all(isinstance(r, RejectionResult) for r in result)

    @pytest.mark.asyncio
    async def test_never_raises_on_exception(self):
        """check_rejected_alternatives returns [] on search failure (never raises)."""
        from smm_sync.context_graph.client import GraphClient

        client = MagicMock(spec=GraphClient)
        client.search_context = AsyncMock(side_effect=RuntimeError("DB exploded"))
        client.check_rejected_alternatives = GraphClient.check_rejected_alternatives.__get__(
            client, GraphClient
        )

        result = await client.check_rejected_alternatives(query="anything", project="test")
        assert result == []


# ---------------------------------------------------------------------------
# query_decisions Déjà Vu integration
# ---------------------------------------------------------------------------

class TestDejaVuInQueryDecisions:
    """Tests for the Déjà Vu warning injected into query_decisions output."""

    def test_deja_vu_warning_shown_when_rejection_found(self):
        """query_decisions output includes DÉJÀ VU WARNING when rejection detected."""
        import asyncio
        import smm_sync.mcp_server as mcp_mod
        from smm_sync.context_graph.models import ContextResult, RejectionResult

        mock_client = MagicMock()
        mock_client.search_context = AsyncMock(return_value=[
            ContextResult(
                title="File locking",
                content="We use os.rename() for atomic locking.",
                relevance_score=0.88,
            )
        ])
        mock_client.check_rejected_alternatives = AsyncMock(return_value=[
            RejectionResult(
                rejected_alternative="Using fcntl advisory locks",
                decision_title="File locking strategy",
                rationale="fcntl advisory locks were rejected because they don't survive across filesystems.",
                decided_at="2025-01-01T00:00:00+00:00",
                confidence=0.90,
                decision_id="abc123",
            )
        ])
        mcp_mod._graph_client = mock_client
        mcp_mod._context_loaded = True

        result = asyncio.run(mcp_mod.query_decisions(query="file locking approach"))
        assert "DÉJÀ VU" in result or "deja vu" in result.lower() or "VU" in result
        mcp_mod._graph_client = None
        mcp_mod._context_loaded = False

    def test_no_deja_vu_warning_when_no_rejections(self):
        """query_decisions output has no warning when no rejections found."""
        import asyncio
        import smm_sync.mcp_server as mcp_mod
        from smm_sync.context_graph.models import ContextResult

        mock_client = MagicMock()
        mock_client.search_context = AsyncMock(return_value=[
            ContextResult(
                title="Kuzu choice",
                content="We use Kuzu for its embedded nature.",
                relevance_score=0.80,
            )
        ])
        mock_client.check_rejected_alternatives = AsyncMock(return_value=[])
        mcp_mod._graph_client = mock_client

        result = asyncio.run(mcp_mod.query_decisions(query="Kuzu"))
        assert "DÉJÀ VU" not in result
        mcp_mod._graph_client = None


# ---------------------------------------------------------------------------
# RejectionResult model
# ---------------------------------------------------------------------------

class TestRejectionResultModel:
    """Tests for the RejectionResult Pydantic model."""

    def test_instantiation(self):
        """RejectionResult can be instantiated with required fields."""
        from smm_sync.context_graph.models import RejectionResult

        r = RejectionResult(
            rejected_alternative="Use SQLite",
            decision_title="Storage choice",
            rationale="SQLite not suitable for graph traversal.",
            decided_at="2025-03-01T00:00:00+00:00",
            confidence=0.85,
            decision_id="decision-xyz",
        )
        assert r.rejected_alternative == "Use SQLite"
        assert r.confidence == 0.85

    def test_confidence_range(self):
        """RejectionResult accepts confidence values between 0 and 1."""
        from smm_sync.context_graph.models import RejectionResult

        r = RejectionResult(
            rejected_alternative="alt",
            decision_title="title",
            rationale="reason",
            decided_at="2025-01-01T00:00:00",
            confidence=0.0,
            decision_id="id",
        )
        assert r.confidence == 0.0

        r2 = RejectionResult(
            rejected_alternative="alt",
            decision_title="title",
            rationale="reason",
            decided_at="2025-01-01T00:00:00",
            confidence=1.0,
            decision_id="id",
        )
        assert r2.confidence == 1.0
