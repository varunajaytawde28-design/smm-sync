"""Tests for the CaaS Dashboard FastAPI backend.

Uses FastAPI TestClient. Mocks GraphClient and file reads so tests run
without a real Kuzu database or ANTHROPIC_API_KEY.
"""
from __future__ import annotations

import json
import tempfile
from datetime import datetime, timezone, timedelta
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from smm_sync.dashboard.app import app, _classify_agents

client = TestClient(app, raise_server_exceptions=True)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_compliance_log(tmp_path: Path, entries: list[dict]) -> Path:
    """Write a compliance_lineage.jsonl to tmp_path/.smm/."""
    smm = tmp_path / ".smm"
    smm.mkdir()
    log = smm / "compliance_lineage.jsonl"
    with open(log, "w") as f:
        for e in entries:
            f.write(json.dumps(e) + "\n")
    return smm


def _injection(
    session_id: str = "sess-1",
    agent: str = "claude-code",
    tool: str = "query_decisions",
    decisions: list[str] | None = None,
    minutes_ago: int = 5,
) -> dict:
    ts = (datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)).isoformat()
    return {
        "entry_id": "abc",
        "timestamp": ts,
        "event_type": "context_injection",
        "agent": agent,
        "tool_name": tool,
        "session_id": session_id,
        "query": "test query",
        "decisions_surfaced": decisions or ["Test Decision"],
        "decision_count": len(decisions or ["Test Decision"]),
    }


# ---------------------------------------------------------------------------
# GET /api/stats
# ---------------------------------------------------------------------------

def test_stats_returns_correct_structure(tmp_path):
    """GET /api/stats returns required keys."""
    smm = _make_compliance_log(tmp_path, [_injection()])

    with patch("smm_sync.dashboard.app._get_smm_dir", return_value=smm):
        r = client.get("/api/stats")

    assert r.status_code == 200
    data = r.json()
    for key in ("decisions", "contradictions", "injections_total",
                "injections_today", "avg_confidence", "active_agents", "captures_today"):
        assert key in data, f"Missing key: {key}"


def test_stats_empty_log(tmp_path):
    """GET /api/stats returns zeros for an empty compliance log."""
    smm = tmp_path / ".smm"
    smm.mkdir()

    with patch("smm_sync.dashboard.app._get_smm_dir", return_value=smm):
        r = client.get("/api/stats")

    assert r.status_code == 200
    data = r.json()
    assert data["injections_total"] == 0
    assert data["injections_today"] == 0
    assert data["captures_today"] == 0


def test_stats_no_smm_dir(tmp_path):
    """GET /api/stats does not crash when .smm/ does not exist."""
    missing = tmp_path / ".smm"

    with patch("smm_sync.dashboard.app._get_smm_dir", return_value=missing):
        r = client.get("/api/stats")

    assert r.status_code == 200


# ---------------------------------------------------------------------------
# GET /api/decisions
# ---------------------------------------------------------------------------

def test_decisions_returns_paginated_empty(tmp_path):
    """GET /api/decisions returns empty list when graph unavailable."""
    smm = tmp_path / ".smm"
    smm.mkdir()

    mock_client = AsyncMock()
    mock_client.get_decisions = AsyncMock(return_value=[])

    with patch("smm_sync.dashboard.app._get_smm_dir", return_value=smm), \
         patch("smm_sync.context_graph.client.GraphClient", return_value=mock_client):
        r = client.get("/api/decisions?limit=20&offset=0")

    assert r.status_code == 200
    data = r.json()
    assert "decisions" in data
    assert "total" in data
    assert "limit" in data
    assert "offset" in data


def test_decisions_pagination_params(tmp_path):
    """GET /api/decisions respects limit and offset params."""
    smm = tmp_path / ".smm"
    smm.mkdir()

    with patch("smm_sync.dashboard.app._get_smm_dir", return_value=smm):
        r = client.get("/api/decisions?limit=5&offset=10")

    assert r.status_code == 200
    data = r.json()
    assert data["limit"] == 5
    assert data["offset"] == 10


# ---------------------------------------------------------------------------
# GET /api/agents + agent classification
# ---------------------------------------------------------------------------

def test_classify_agents_active():
    """Agents with injections in last 60 min are 'active'."""
    entries = [_injection(session_id="sess-active", minutes_ago=10)]
    agents = _classify_agents(entries)
    assert len(agents) == 1
    assert agents[0]["status"] == "active"
    assert agents[0]["session_id"] == "sess-active"


def test_classify_agents_idle():
    """Agents with injections between 1-24 hours ago are 'idle'."""
    entries = [_injection(session_id="sess-idle", minutes_ago=120)]
    agents = _classify_agents(entries)
    assert agents[0]["status"] == "idle"


def test_classify_agents_disconnected():
    """Agents with injections older than 24 hours are 'disconnected'."""
    entries = [_injection(session_id="sess-old", minutes_ago=1500)]
    agents = _classify_agents(entries)
    assert agents[0]["status"] == "disconnected"


def test_agents_endpoint_empty_log(tmp_path):
    """GET /api/agents returns empty list when no compliance log exists."""
    smm = tmp_path / ".smm"
    smm.mkdir()

    with patch("smm_sync.dashboard.app._get_smm_dir", return_value=smm):
        r = client.get("/api/agents")

    assert r.status_code == 200
    assert r.json()["agents"] == []


def test_agents_endpoint_with_entries(tmp_path):
    """GET /api/agents returns classified agents from compliance log."""
    smm = _make_compliance_log(tmp_path, [
        _injection(session_id="s1", agent="claude-code", minutes_ago=5),
        _injection(session_id="s1", agent="claude-code", minutes_ago=3),
        _injection(session_id="s2", agent="cursor", minutes_ago=200),
    ])

    with patch("smm_sync.dashboard.app._get_smm_dir", return_value=smm):
        r = client.get("/api/agents")

    assert r.status_code == 200
    agents = r.json()["agents"]
    assert len(agents) == 2
    statuses = {a["session_id"]: a["status"] for a in agents}
    assert statuses["s1"] == "active"
    assert statuses["s2"] == "idle"


# ---------------------------------------------------------------------------
# POST /api/agents/{session_id}/disconnect
# ---------------------------------------------------------------------------

def test_disconnect_writes_killed_sessions(tmp_path):
    """POST /api/agents/{id}/disconnect writes to killed_sessions.json."""
    smm = tmp_path / ".smm"
    smm.mkdir()

    with patch("smm_sync.dashboard.app._get_smm_dir", return_value=smm):
        r = client.post("/api/agents/my-session-123/disconnect")

    assert r.status_code == 200
    assert r.json()["success"] is True

    killed_path = smm / "killed_sessions.json"
    assert killed_path.exists()
    data = json.loads(killed_path.read_text())
    assert "my-session-123" in data["sessions"]


def test_disconnect_idempotent(tmp_path):
    """POST /api/agents/{id}/disconnect is idempotent (no duplicate entries)."""
    smm = tmp_path / ".smm"
    smm.mkdir()

    with patch("smm_sync.dashboard.app._get_smm_dir", return_value=smm):
        client.post("/api/agents/sess-abc/disconnect")
        r = client.post("/api/agents/sess-abc/disconnect")

    assert r.status_code == 200
    killed = json.loads((smm / "killed_sessions.json").read_text())
    assert killed["sessions"].count("sess-abc") == 1


# ---------------------------------------------------------------------------
# POST /api/query
# ---------------------------------------------------------------------------

def test_query_returns_503_without_api_key(tmp_path):
    """POST /api/query returns 503 if ANTHROPIC_API_KEY is not set."""
    import os
    smm = tmp_path / ".smm"
    smm.mkdir()

    with patch("smm_sync.dashboard.app._get_smm_dir", return_value=smm), \
         patch.dict(os.environ, {}, clear=True):
        # Ensure ANTHROPIC_API_KEY is absent
        os.environ.pop("ANTHROPIC_API_KEY", None)
        r = client.post("/api/query", json={"query": "test", "limit": 5})

    assert r.status_code == 503
    assert "ANTHROPIC_API_KEY" in r.json()["detail"]


def test_query_returns_results(tmp_path):
    """POST /api/query returns results from GraphClient."""
    import os
    from smm_sync.context_graph.models import ContextResult

    smm = tmp_path / ".smm"
    smm.mkdir()

    mock_result = ContextResult(
        title="Test Decision",
        content="Some content",
        relevance_score=0.9,
        excerpt="Some content",
    )
    mock_client = AsyncMock()
    mock_client.search_context = AsyncMock(return_value=[mock_result])

    with patch("smm_sync.dashboard.app._get_smm_dir", return_value=smm), \
         patch.dict(os.environ, {"ANTHROPIC_API_KEY": "sk-test"}), \
         patch("smm_sync.context_graph.client.GraphClient", return_value=mock_client):
        r = client.post("/api/query", json={"query": "test", "limit": 5})

    assert r.status_code == 200
    data = r.json()
    assert "results" in data
    assert "query" in data
    assert "took_ms" in data


# ---------------------------------------------------------------------------
# GET /api/capture/status
# ---------------------------------------------------------------------------

def test_capture_status_no_state_file(tmp_path):
    """GET /api/capture/status returns empty state when no capture_state.json."""
    smm = tmp_path / ".smm"
    smm.mkdir()

    with patch("smm_sync.dashboard.app._get_smm_dir", return_value=smm):
        r = client.get("/api/capture/status")

    assert r.status_code == 200
    data = r.json()
    assert data["last_run"] is None
    assert data["repos"] == []


def test_capture_status_reads_state_file(tmp_path):
    """GET /api/capture/status reads from capture_state.json."""
    smm = tmp_path / ".smm"
    smm.mkdir()
    state = {
        "myorg/myrepo": {
            "last_run": "2026-03-22T18:00:00+00:00",
            "last_pr_number": 48,
            "last_commit_sha": "abc123def456",
        }
    }
    (smm / "capture_state.json").write_text(json.dumps(state))

    with patch("smm_sync.dashboard.app._get_smm_dir", return_value=smm):
        r = client.get("/api/capture/status")

    assert r.status_code == 200
    data = r.json()
    assert data["last_run"] is not None
    assert len(data["repos"]) == 1
    assert data["repos"][0]["name"] == "myrepo"
    assert data["repos"][0]["last_pr"] == 48


# ---------------------------------------------------------------------------
# GET /api/contradictions
# ---------------------------------------------------------------------------

def test_contradictions_empty(tmp_path):
    """GET /api/contradictions returns empty list when no file."""
    smm = tmp_path / ".smm"
    smm.mkdir()

    with patch("smm_sync.dashboard.app._get_smm_dir", return_value=smm):
        r = client.get("/api/contradictions")

    assert r.status_code == 200
    assert r.json()["contradictions"] == []


def test_contradictions_with_entries(tmp_path):
    """GET /api/contradictions returns entries from contradictions.jsonl."""
    smm = tmp_path / ".smm"
    smm.mkdir()
    contradiction = {
        "id": "c1", "decision_a": "A", "decision_b": "B",
        "explanation": "conflict", "detected_at": "2026-03-22T10:00:00Z",
        "resolved": False,
    }
    (smm / "contradictions.jsonl").write_text(json.dumps(contradiction) + "\n")

    with patch("smm_sync.dashboard.app._get_smm_dir", return_value=smm):
        r = client.get("/api/contradictions")

    assert r.status_code == 200
    assert len(r.json()["contradictions"]) == 1


# ---------------------------------------------------------------------------
# GET /api/compliance
# ---------------------------------------------------------------------------

def test_compliance_returns_entries(tmp_path):
    """GET /api/compliance returns entries from compliance log."""
    smm = _make_compliance_log(tmp_path, [
        _injection(session_id="s1", minutes_ago=5),
        _injection(session_id="s2", minutes_ago=10),
    ])

    with patch("smm_sync.dashboard.app._get_smm_dir", return_value=smm):
        r = client.get("/api/compliance?limit=20")

    assert r.status_code == 200
    data = r.json()
    assert data["total"] == 2
    assert len(data["entries"]) == 2


def test_compliance_filter_by_session(tmp_path):
    """GET /api/compliance?session_id= filters correctly."""
    smm = _make_compliance_log(tmp_path, [
        _injection(session_id="s1"),
        _injection(session_id="s2"),
    ])

    with patch("smm_sync.dashboard.app._get_smm_dir", return_value=smm):
        r = client.get("/api/compliance?session_id=s1")

    assert r.status_code == 200
    assert r.json()["total"] == 1


# ---------------------------------------------------------------------------
# GET /api/graph
# ---------------------------------------------------------------------------

def test_graph_returns_empty_on_unavailable_graph(tmp_path):
    """GET /api/graph returns empty nodes/edges when graph unavailable."""
    smm = tmp_path / ".smm"
    smm.mkdir()

    with patch("smm_sync.dashboard.app._get_smm_dir", return_value=smm):
        r = client.get("/api/graph")

    assert r.status_code == 200
    data = r.json()
    assert "nodes" in data
    assert "edges" in data
