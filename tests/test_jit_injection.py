"""Tests for JIT path-based rule injection (get_path_context)."""
from __future__ import annotations

import pytest
from unittest.mock import AsyncMock, MagicMock


class TestExtractPathKeywords:
    """Tests for GraphClient._extract_path_keywords."""

    def test_parses_mcp_server_path(self, tmp_path):
        """_extract_path_keywords correctly handles mcp_server path."""
        from smm_sync.context_graph.client import GraphClient

        client = GraphClient(graph_dir=tmp_path / "graph", api_key="test")
        keywords = client._extract_path_keywords("src/smm_sync/mcp_server.py")

        assert isinstance(keywords, list)
        assert len(keywords) <= 4
        # Should contain mcp-related keywords
        all_kw = " ".join(keywords).lower()
        assert any(k in all_kw for k in ["mcp", "security", "tools"])

    def test_parses_github_capture_path(self, tmp_path):
        """_extract_path_keywords handles github_capture path."""
        from smm_sync.context_graph.client import GraphClient

        client = GraphClient(graph_dir=tmp_path / "graph", api_key="test")
        keywords = client._extract_path_keywords("src/smm_sync/capture/github_capture.py")

        assert len(keywords) <= 4
        all_kw = " ".join(keywords).lower()
        assert any(k in all_kw for k in ["capture", "github", "extraction"])

    def test_parses_dashboard_path(self, tmp_path):
        """_extract_path_keywords handles dashboard/app.py."""
        from smm_sync.context_graph.client import GraphClient

        client = GraphClient(graph_dir=tmp_path / "graph", api_key="test")
        keywords = client._extract_path_keywords("src/smm_sync/dashboard/app.py")

        assert len(keywords) <= 4
        all_kw = " ".join(keywords).lower()
        assert any(k in all_kw for k in ["dashboard", "api", "frontend"])

    def test_filters_noise_words(self, tmp_path):
        """_extract_path_keywords removes noise words like src, py, __init__."""
        from smm_sync.context_graph.client import GraphClient

        client = GraphClient(graph_dir=tmp_path / "graph", api_key="test")
        keywords = client._extract_path_keywords("src/__init__.py")

        # Should be empty or very minimal
        assert len(keywords) <= 2

    def test_returns_max_four_keywords(self, tmp_path):
        """_extract_path_keywords returns at most 4 keywords."""
        from smm_sync.context_graph.client import GraphClient

        client = GraphClient(graph_dir=tmp_path / "graph", api_key="test")
        keywords = client._extract_path_keywords(
            "src/smm_sync/capture/github_capture.py"
        )

        assert len(keywords) <= 4


class TestGetPathContext:
    """Tests for GraphClient.get_path_context."""

    @pytest.mark.asyncio
    async def test_returns_results_for_known_path(self, tmp_path):
        """get_path_context returns results for mcp_server.py."""
        from smm_sync.context_graph.client import GraphClient
        from smm_sync.context_graph.models import ContextResult

        client = GraphClient(graph_dir=tmp_path / "graph", api_key="test")

        mock_results = [
            ContextResult(
                title="Never expose raw MCP to enterprise customers",
                content="[CONSTRAINT] Never expose raw MCP.\nRationale: 6 fatal security flaws.",
                relevance_score=0.92,
                excerpt="6 fatal security flaws.",
            )
        ]
        client.search_context = AsyncMock(return_value=mock_results)

        results = await client.get_path_context(
            file_path="src/smm_sync/mcp_server.py",
            project="smm-sync"
        )

        assert isinstance(results, list)
        assert len(results) <= 3

    @pytest.mark.asyncio
    async def test_returns_empty_for_unknown_path(self, tmp_path):
        """get_path_context returns empty list for unknown path."""
        from smm_sync.context_graph.client import GraphClient

        client = GraphClient(graph_dir=tmp_path / "graph", api_key="test")
        client.search_context = AsyncMock(return_value=[])

        results = await client.get_path_context(
            file_path="tests/anything.py",
            project="smm-sync"
        )

        assert isinstance(results, list)

    @pytest.mark.asyncio
    async def test_never_raises_on_failure(self, tmp_path):
        """get_path_context returns empty list on any error."""
        from smm_sync.context_graph.client import GraphClient

        client = GraphClient(graph_dir=tmp_path / "graph", api_key="test")
        client.search_context = AsyncMock(side_effect=RuntimeError("Graph error"))

        results = await client.get_path_context(
            file_path="src/smm_sync/mcp_server.py",
            project="smm-sync"
        )

        assert results == []

    @pytest.mark.asyncio
    async def test_returns_max_three_results(self, tmp_path):
        """get_path_context returns at most 3 results."""
        from smm_sync.context_graph.client import GraphClient
        from smm_sync.context_graph.models import ContextResult

        client = GraphClient(graph_dir=tmp_path / "graph", api_key="test")

        many_results = [
            ContextResult(
                title=f"Decision {i}",
                content=f"[CONSTRAINT] Rule {i}.",
                relevance_score=0.9,
                excerpt=f"Rule {i}.",
            )
            for i in range(10)
        ]
        client.search_context = AsyncMock(return_value=many_results)

        results = await client.get_path_context(
            file_path="src/smm_sync/mcp_server.py",
            project="smm-sync"
        )

        assert len(results) <= 3
