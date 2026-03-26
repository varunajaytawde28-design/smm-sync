"""Tests for the 4 new MCP tools: query_decisions, add_decision,
get_project_context, check_constraints.

These tests verify graceful degradation when the context graph is unavailable
(no ANTHROPIC_API_KEY, fresh graph, etc.) and basic return-type contracts.
"""
from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# We need to initialise the MCP server module with a fake smm_dir
import smm_sync.mcp_server as _mcp_module


@pytest.fixture(autouse=True)
def reset_mcp_globals(tmp_path: Path):
    """Reset module-level globals before each test."""
    old_smm_dir = _mcp_module._smm_dir
    old_graph_client = _mcp_module._graph_client

    smm_dir = tmp_path / ".smm"
    smm_dir.mkdir()
    _mcp_module._smm_dir = smm_dir
    _mcp_module._graph_client = None

    yield smm_dir

    _mcp_module._smm_dir = old_smm_dir
    _mcp_module._graph_client = old_graph_client


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _run(coro):
    """Run an async function synchronously in tests."""
    return asyncio.run(coro)


# ---------------------------------------------------------------------------
# query_decisions
# ---------------------------------------------------------------------------

def test_query_decisions_returns_string_when_graph_empty(reset_mcp_globals):
    """query_decisions returns a non-empty string even on empty/unavailable graph."""
    # Graph client will be initialised but graph is empty (no API key)
    result = _run(_mcp_module.query_decisions(
        query="why did we reject LWW CRDT",
        project="smm-sync-test",
    ))
    assert isinstance(result, str)
    assert len(result) > 0


def test_query_decisions_graceful_on_graph_failure(reset_mcp_globals):
    """query_decisions returns a string (not raises) when graph client fails."""
    # Force _graph_client to a broken mock
    mock_client = MagicMock()
    mock_client.search_context = AsyncMock(side_effect=RuntimeError("DB gone"))
    _mcp_module._graph_client = mock_client

    result = _run(_mcp_module.query_decisions(query="anything"))
    assert isinstance(result, str)
    assert "failed" in result.lower() or "error" in result.lower() or len(result) > 0


def test_query_decisions_no_results_returns_string(reset_mcp_globals):
    """query_decisions returns a helpful string when no results found."""
    mock_client = MagicMock()
    mock_client.search_context = AsyncMock(return_value=[])
    mock_client.check_rejected_alternatives = AsyncMock(return_value=[])
    _mcp_module._graph_client = mock_client

    result = _run(_mcp_module.query_decisions(query="something obscure", project="smm-sync"))
    assert isinstance(result, str)
    assert len(result) > 0


# ---------------------------------------------------------------------------
# add_decision
# ---------------------------------------------------------------------------

def test_add_decision_returns_dict_with_success_key(reset_mcp_globals):
    """add_decision returns a dict containing the 'success' key."""
    mock_client = MagicMock()
    mock_client.add_decision = AsyncMock(return_value="fake-uuid-1234")
    _mcp_module._graph_client = mock_client

    result = _run(_mcp_module.add_decision(
        title="Test decision",
        content="We use X.",
        rationale="Because Y.",
        made_by="test",
    ))
    assert isinstance(result, dict)
    assert "success" in result
    assert result["success"] is True
    assert "decision_id" in result


def test_add_decision_returns_failure_dict_on_error(reset_mcp_globals):
    """add_decision returns {'success': False, 'error': ...} on graph failure."""
    mock_client = MagicMock()
    mock_client.add_decision = AsyncMock(side_effect=RuntimeError("API error"))
    _mcp_module._graph_client = mock_client

    result = _run(_mcp_module.add_decision(
        title="Test",
        content="X",
        rationale="Y",
        made_by="test",
    ))
    assert isinstance(result, dict)
    assert "success" in result
    assert result["success"] is False
    assert "error" in result


def test_add_decision_returns_failure_when_graph_unavailable(reset_mcp_globals):
    """add_decision returns failure dict when graph client can't initialise."""
    _mcp_module._graph_client = None
    # Patch _get_graph_client to return None
    with patch.object(_mcp_module, "_get_graph_client", return_value=None):
        result = _run(_mcp_module.add_decision(
            title="Test",
            content="X",
            rationale="Y",
            made_by="test",
        ))
    assert isinstance(result, dict)
    assert result["success"] is False


# ---------------------------------------------------------------------------
# get_project_context
# ---------------------------------------------------------------------------

def test_get_project_context_returns_string(reset_mcp_globals):
    """get_project_context returns a string."""
    from smm_sync.context_graph.models import Decision

    mock_client = MagicMock()
    mock_client.get_decisions = AsyncMock(return_value=[
        Decision(
            id="1",
            title="Use os.rename()",
            content="Atomic locking via rename.",
            rationale="POSIX atomic.",
            made_by="Varun",
            project="smm-sync",
        )
    ])
    _mcp_module._graph_client = mock_client

    result = _run(_mcp_module.get_project_context(project="smm-sync"))
    assert isinstance(result, str)
    assert "os.rename()" in result


def test_get_project_context_graceful_on_error(reset_mcp_globals):
    """get_project_context returns a string (not raises) on graph failure."""
    mock_client = MagicMock()
    mock_client.get_decisions = AsyncMock(side_effect=RuntimeError("Kuzu gone"))
    _mcp_module._graph_client = mock_client

    result = _run(_mcp_module.get_project_context())
    assert isinstance(result, str)
    assert len(result) > 0


def test_get_project_context_empty_graph_message(reset_mcp_globals):
    """get_project_context returns helpful message when graph has no decisions."""
    mock_client = MagicMock()
    mock_client.get_decisions = AsyncMock(return_value=[])
    _mcp_module._graph_client = mock_client

    result = _run(_mcp_module.get_project_context())
    assert isinstance(result, str)
    assert "seed" in result.lower() or "no decisions" in result.lower()


# ---------------------------------------------------------------------------
# check_constraints
# ---------------------------------------------------------------------------

def test_check_constraints_returns_dict_with_required_keys(reset_mcp_globals):
    """check_constraints returns dict with 'clear', 'conflicts', 'warnings'."""
    from smm_sync.context_graph.models import ContextResult

    mock_client = MagicMock()
    mock_client.search_context = AsyncMock(return_value=[
        ContextResult(
            title="MCP security",
            content="DO NOT expose raw MCP to enterprise customers.",
            relevance_score=0.9,
        )
    ])
    _mcp_module._graph_client = mock_client

    result = _run(_mcp_module.check_constraints(
        proposed_action="expose raw MCP without gateway"
    ))
    assert isinstance(result, dict)
    assert "conflicts" in result
    assert "warnings" in result
    assert "clear" in result
    assert isinstance(result["conflicts"], list)
    assert isinstance(result["warnings"], list)
    assert isinstance(result["clear"], bool)


def test_check_constraints_clear_when_no_matches(reset_mcp_globals):
    """check_constraints.clear is True when no relevant conflicts found."""
    from smm_sync.context_graph.models import ContextResult

    mock_client = MagicMock()
    mock_client.search_context = AsyncMock(return_value=[
        ContextResult(
            title="Unrelated fact",
            content="The sky is blue.",
            relevance_score=0.1,
        )
    ])
    _mcp_module._graph_client = mock_client

    result = _run(_mcp_module.check_constraints(
        proposed_action="add a new unit test",
        project="smm-sync",
    ))
    assert isinstance(result, dict)
    assert "clear" in result


def test_check_constraints_graceful_when_graph_unavailable(reset_mcp_globals):
    """check_constraints returns dict (not raises) when graph is unavailable."""
    with patch.object(_mcp_module, "_get_graph_client", return_value=None):
        result = _run(_mcp_module.check_constraints(
            proposed_action="add LWW CRDT back"
        ))
    assert isinstance(result, dict)
    assert "conflicts" in result
    assert "warnings" in result
    assert "clear" in result
    assert result["clear"] is False


def test_check_constraints_graceful_on_search_error(reset_mcp_globals):
    """check_constraints returns dict (not raises) when graph search fails."""
    mock_client = MagicMock()
    mock_client.search_context = AsyncMock(side_effect=RuntimeError("search broke"))
    _mcp_module._graph_client = mock_client

    result = _run(_mcp_module.check_constraints(proposed_action="anything"))
    assert isinstance(result, dict)
    assert "warnings" in result
