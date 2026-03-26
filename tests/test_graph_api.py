"""Tests for the /api/graph endpoint — source provenance and node sizing."""
from __future__ import annotations

import json
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from smm_sync.dashboard.app import app

client = TestClient(app, raise_server_exceptions=True)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_decision(
    id: str = "uuid-1",
    title: str = "Use Kuzu for dev",
    content: str = "",
) -> MagicMock:
    d = MagicMock()
    d.id = id
    d.title = title
    d.content = content
    d.created_at = None
    return d


def _mock_graph_client(decisions):
    """Return a mock graph client whose get_decisions returns *decisions*."""
    mock = MagicMock()
    mock.get_decisions = AsyncMock(return_value=decisions)
    return mock


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_graph_endpoint_includes_source_pr():
    """Nodes from a github_pr source expose source_pr in the API response."""
    content = (
        "Confidence: 0.90\n"
        "Rationale: Chosen for performance.\n"
        "Decision type: technical\n"
        "Source type: github_pr\n"
        "Source PR: 47\n"
    )
    decision = _make_decision(content=content)

    with (
        patch("smm_sync.dashboard.app._get_smm_dir", return_value=Path(tempfile.mkdtemp())),
        patch("smm_sync.dashboard.app._get_graph_client", return_value=_mock_graph_client([decision])),
    ):
        resp = client.get("/api/graph")

    assert resp.status_code == 200
    data = resp.json()
    nodes = data["nodes"]
    assert len(nodes) == 1
    assert nodes[0]["source_pr"] == "47"


def test_graph_endpoint_includes_source_url():
    """source_url field is present in graph node (None when not explicitly set)."""
    decision = _make_decision(content="Confidence: 0.80\nRationale: test.\n")

    with (
        patch("smm_sync.dashboard.app._get_smm_dir", return_value=Path(tempfile.mkdtemp())),
        patch("smm_sync.dashboard.app._get_graph_client", return_value=_mock_graph_client([decision])),
    ):
        resp = client.get("/api/graph")

    assert resp.status_code == 200
    nodes = resp.json()["nodes"]
    assert "source_url" in nodes[0]


def test_graph_node_size_reflects_confidence():
    """Higher-confidence nodes get a larger size value (40 + confidence*30)."""
    high = _make_decision(id="h", title="High confidence", content="Confidence: 0.95\n")
    low = _make_decision(id="l", title="Low confidence", content="Confidence: 0.50\n")

    with (
        patch("smm_sync.dashboard.app._get_smm_dir", return_value=Path(tempfile.mkdtemp())),
        patch("smm_sync.dashboard.app._get_graph_client", return_value=_mock_graph_client([high, low])),
    ):
        resp = client.get("/api/graph")

    assert resp.status_code == 200
    nodes = {n["id"]: n for n in resp.json()["nodes"]}
    # The API returns confidence; frontend computes size as 40 + confidence*30
    assert nodes["h"]["confidence"] > nodes["l"]["confidence"]


def test_graph_handles_missing_source_gracefully():
    """Nodes without source_pr or source_type fields don't crash the endpoint."""
    decision = _make_decision(content="Confidence: 0.75\nRationale: no source metadata.\n")

    with (
        patch("smm_sync.dashboard.app._get_smm_dir", return_value=Path(tempfile.mkdtemp())),
        patch("smm_sync.dashboard.app._get_graph_client", return_value=_mock_graph_client([decision])),
    ):
        resp = client.get("/api/graph")

    assert resp.status_code == 200
    nodes = resp.json()["nodes"]
    assert len(nodes) == 1
    assert nodes[0]["source_pr"] is None
    assert nodes[0]["source_url"] is None
    assert nodes[0]["source_type"] == "manual"
