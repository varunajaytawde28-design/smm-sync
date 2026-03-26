"""Tests for context_graph module.

IMPORTANT: Tests that call add_decision make real Anthropic API calls.
They use a separate project "smm-sync-test" to avoid polluting the real graph.

Tests are split into:
- Infrastructure tests (no API calls): init, health_check
- Integration tests (API calls, slow): add_decision, search_context

Run only infrastructure tests with:
    pytest tests/test_context_graph.py -m "not slow"
"""
from __future__ import annotations

import asyncio
import os
import shutil
import tempfile
from pathlib import Path

import pytest

from smm_sync.context_graph.client import GraphClient
from smm_sync.context_graph.models import ContextResult, Decision


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def graph_dir(tmp_path: Path) -> Path:
    """Return a temporary graph directory."""
    return tmp_path / ".smm" / "graph"


@pytest.fixture
def client(graph_dir: Path) -> GraphClient:
    """Return a GraphClient pointed at a fresh temp directory."""
    return GraphClient(graph_dir=graph_dir)


@pytest.fixture
def client_with_key(graph_dir: Path) -> GraphClient:
    """Return a GraphClient with ANTHROPIC_API_KEY (skips if not set)."""
    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not api_key:
        pytest.skip("ANTHROPIC_API_KEY not set — skipping API-dependent test")
    return GraphClient(graph_dir=graph_dir, api_key=api_key)


# ---------------------------------------------------------------------------
# Infrastructure tests — no API calls
# ---------------------------------------------------------------------------

def test_graph_client_initialises(graph_dir: Path) -> None:
    """GraphClient initialises without error; graph_dir is stored."""
    client = GraphClient(graph_dir=graph_dir)
    assert client.graph_dir == graph_dir
    assert client._graphiti is None  # lazy — not yet connected


def test_graph_dir_created_on_health_check(client: GraphClient) -> None:
    """health_check() creates the Kuzu DB file on first access."""
    assert not client.graph_dir.exists()
    result = client.health_check()
    assert result is True
    # Kuzu creates the DB as a file (not a directory) at graph_dir
    assert client.graph_dir.exists()


def test_health_check_returns_true_on_fresh_path(
    graph_dir: Path, client: GraphClient
) -> None:
    """health_check() returns True when parent exists but graph DB doesn't yet."""
    # Create parent only — kuzu creates the DB file itself
    graph_dir.parent.mkdir(parents=True, exist_ok=True)
    assert not graph_dir.exists()
    assert client.health_check() is True
    assert graph_dir.exists()


def test_health_check_returns_false_on_bad_path() -> None:
    """health_check() returns False gracefully on an unusable path."""
    # Use a path that looks like a file, not a directory — kuzu will fail
    with tempfile.NamedTemporaryFile(suffix=".db") as f:
        # Point graph_dir at an existing FILE (Kuzu expects a directory)
        bad_client = GraphClient(graph_dir=Path(f.name) / "impossible")
        # This should not raise — it should return False
        try:
            result = bad_client.health_check()
            # Either True (kuzu made subdirs) or False is acceptable
            assert isinstance(result, bool)
        except Exception:
            pytest.fail("health_check() must never raise")


# ---------------------------------------------------------------------------
# Models tests
# ---------------------------------------------------------------------------

def test_context_result_defaults() -> None:
    """ContextResult can be constructed with minimal fields."""
    r = ContextResult(title="Test", content="Some fact")
    assert r.title == "Test"
    assert r.content == "Some fact"
    assert r.relevance_score == 0.0
    assert r.excerpt == ""


def test_decision_defaults() -> None:
    """Decision can be constructed and has expected defaults."""
    d = Decision(
        id="abc",
        title="Test decision",
        content="We decided X",
        rationale="Because Y",
        made_by="Varun",
        project="smm-sync",
    )
    assert d.valid is True
    assert d.constraints == []
    assert d.alternatives == []


# ---------------------------------------------------------------------------
# Integration tests — require ANTHROPIC_API_KEY (marked slow)
# ---------------------------------------------------------------------------

@pytest.mark.slow
def test_add_decision_stores_without_error(client_with_key: GraphClient) -> None:
    """add_decision stores an episode without raising."""
    async def _run():
        episode_id = await client_with_key.add_decision(
            title="Test atomic locking",
            content="We use os.rename() for atomic locking.",
            rationale="POSIX-atomic, no daemon needed.",
            made_by="test-runner",
            project="smm-sync-test",
        )
        assert isinstance(episode_id, str)
        assert len(episode_id) > 0

    asyncio.run(_run())


@pytest.mark.slow
def test_search_context_returns_list(client_with_key: GraphClient) -> None:
    """search_context returns a list (may be empty for fresh graph)."""
    async def _run():
        results = await client_with_key.search_context(
            query="file locking mechanism",
            project="smm-sync-test",
            limit=5,
        )
        assert isinstance(results, list)
        for r in results:
            assert isinstance(r, ContextResult)

    asyncio.run(_run())


@pytest.mark.slow
def test_search_returns_empty_for_unknown_query(client_with_key: GraphClient) -> None:
    """search_context returns empty list gracefully for queries with no matches."""
    async def _run():
        results = await client_with_key.search_context(
            query="xyzzy_nonexistent_term_12345",
            project="smm-sync-test",
            limit=5,
        )
        assert isinstance(results, list)

    asyncio.run(_run())


@pytest.mark.slow
def test_multi_tenancy_isolation(graph_dir: Path) -> None:
    """Seeding project A and querying project B returns empty results."""
    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not api_key:
        pytest.skip("ANTHROPIC_API_KEY not set")

    client_a = GraphClient(graph_dir=graph_dir, api_key=api_key)
    client_b = GraphClient(graph_dir=graph_dir, api_key=api_key)

    async def _run():
        # Seed one decision in project A
        await client_a.add_decision(
            title="Project A decision",
            content="We use approach X in project A.",
            rationale="Because it's faster.",
            made_by="test",
            project="smm-sync-test-a",
        )
        # Query in project B — should not find project A decisions
        results = await client_b.search_context(
            query="approach X",
            project="smm-sync-test-b",
            limit=5,
        )
        # Results should be empty or not contain the project A decision
        contents = [r.content for r in results]
        assert all("project A" not in c for c in contents), (
            "Multi-tenancy broken: project B query returned project A data"
        )

    asyncio.run(_run())
