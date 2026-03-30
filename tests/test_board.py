"""Tests for the Decision Board feature."""
from __future__ import annotations

import json
import uuid
import pytest
from unittest.mock import AsyncMock, patch
from fastapi.testclient import TestClient


@pytest.fixture
def client():
    """Return a test client for the dashboard app."""
    from smm_sync.dashboard.app import app
    return TestClient(app)


@pytest.fixture
def board_dir(tmp_path):
    """Create a temp .smm directory (board now reads from contradictions.jsonl)."""
    smm_dir = tmp_path / ".smm"
    smm_dir.mkdir()
    return smm_dir


class TestBoardCRUD:
    """Tests for the board API (single source of truth: contradictions.jsonl)."""

    def test_get_empty_board(self, client, board_dir):
        """GET /api/board returns empty grouped dict when no contradictions exist."""
        with patch("smm_sync.dashboard.app._get_smm_dir", return_value=board_dir):
            response = client.get("/api/board")
        assert response.status_code == 200
        data = response.json()
        assert data["items"] == []
        assert "backlog" in data["grouped"]

    def test_board_reads_from_contradictions_jsonl(self, client, board_dir):
        """GET /api/board derives items from contradictions.jsonl, not board.json."""
        cid = str(uuid.uuid4())
        contradictions = [
            {
                "id": cid,
                "decision_a": "Use PostgreSQL",
                "decision_b": "Use SQLite",
                "explanation": "Storage conflict",
                "resolved": False,
                "status": "pending",
                "detected_at": "2026-01-01T00:00:00Z",
            }
        ]
        (board_dir / "contradictions.jsonl").write_text(
            "\n".join(json.dumps(c) for c in contradictions)
        )

        with patch("smm_sync.dashboard.app._get_smm_dir", return_value=board_dir):
            response = client.get("/api/board")

        assert response.status_code == 200
        data = response.json()
        assert len(data["items"]) == 1
        item = data["items"][0]
        assert item["id"] == cid
        assert item["_contradiction_id"] == cid
        assert item["status"] == "backlog"
        assert len(data["grouped"]["backlog"]) == 1
        assert len(data["grouped"]["done"]) == 0

    def test_resolved_contradiction_appears_in_done(self, client, board_dir):
        """Resolved contradictions map to Done column with Resolved badge fields."""
        cid = str(uuid.uuid4())
        contradictions = [
            {
                "id": cid,
                "decision_a": "Use Redis",
                "decision_b": "Use Memcached",
                "explanation": "Caching conflict",
                "resolved": True,
                "status": "resolved",
                "winner": "Use Redis",
                "note": "Redis has better persistence",
                "detected_at": "2026-01-01T00:00:00Z",
                "resolved_at": "2026-01-02T00:00:00Z",
            }
        ]
        (board_dir / "contradictions.jsonl").write_text(
            "\n".join(json.dumps(c) for c in contradictions)
        )

        with patch("smm_sync.dashboard.app._get_smm_dir", return_value=board_dir):
            response = client.get("/api/board")

        assert response.status_code == 200
        data = response.json()
        assert len(data["grouped"]["done"]) == 1
        assert len(data["grouped"]["backlog"]) == 0
        item = data["grouped"]["done"][0]
        assert item["_resolved_winner"] == "Use Redis"
        assert "_dismissed" not in item

    def test_dismissed_contradiction_appears_in_done(self, client, board_dir):
        """Dismissed/ignored contradictions map to Done column with Dismissed badge."""
        cid = str(uuid.uuid4())
        contradictions = [
            {
                "id": cid,
                "decision_a": "Use A",
                "decision_b": "Use B",
                "explanation": "Not a real conflict",
                "resolved": True,
                "status": "ignored",
                "detected_at": "2026-01-01T00:00:00Z",
            }
        ]
        (board_dir / "contradictions.jsonl").write_text(
            "\n".join(json.dumps(c) for c in contradictions)
        )

        with patch("smm_sync.dashboard.app._get_smm_dir", return_value=board_dir):
            response = client.get("/api/board")

        assert response.status_code == 200
        data = response.json()
        assert len(data["grouped"]["done"]) == 1
        item = data["grouped"]["done"][0]
        assert item.get("_dismissed") is True

    def test_create_item_returns_501(self, client, board_dir):
        """POST /api/board returns 501 — manual items not supported."""
        with patch("smm_sync.dashboard.app._get_smm_dir", return_value=board_dir):
            response = client.post("/api/board", json={
                "title": "Manual task",
                "type": "task",
            })
        assert response.status_code == 501

    def test_update_item_noop(self, client, board_dir):
        """PATCH /api/board/{id} is a no-op that returns 200."""
        item_id = str(uuid.uuid4())
        with patch("smm_sync.dashboard.app._get_smm_dir", return_value=board_dir):
            r1 = client.patch(f"/api/board/{item_id}", json={"status": "in_progress"})
            r2 = client.patch(f"/api/board/{item_id}", json={"status": "in_progress"})

        assert r1.status_code == 200
        assert r2.status_code == 200

    def test_delete_item_returns_501(self, client, board_dir):
        """DELETE /api/board/{id} returns 501 — use contradictions API instead."""
        item_id = str(uuid.uuid4())
        with patch("smm_sync.dashboard.app._get_smm_dir", return_value=board_dir):
            response = client.delete(f"/api/board/{item_id}")
        assert response.status_code == 501

    def test_resolve_board_item_creates_graph_decision(self, client, board_dir):
        """POST /api/board/{id}/resolve resolves the contradiction and writes a decision."""
        cid = str(uuid.uuid4())
        contradictions = [
            {
                "id": cid,
                "decision_a": "Migrate DB",
                "decision_b": "Stay on current",
                "explanation": "Migration question",
                "resolved": False,
                "status": "pending",
                "detected_at": "2026-01-01T00:00:00Z",
            }
        ]
        (board_dir / "contradictions.jsonl").write_text(json.dumps(contradictions[0]))

        with patch("smm_sync.dashboard.app._get_smm_dir", return_value=board_dir):
            response = client.post(f"/api/board/{cid}/resolve", json={
                "decision": "We will migrate to FalkorDB",
                "rationale": "Docker now available",
                "alternatives": ["Stay on Kuzu"],
            })

        assert response.status_code == 200
        data = response.json()
        assert data["success"] is True
        assert "decision_id" in data

    def test_resolve_board_item_idempotent(self, client, board_dir):
        """POST /api/board/{id}/resolve with unknown id returns idempotent=True."""
        item_id = str(uuid.uuid4())
        # No contradictions.jsonl — id won't be found
        with patch("smm_sync.dashboard.app._get_smm_dir", return_value=board_dir):
            response = client.post(f"/api/board/{item_id}/resolve", json={
                "decision": "Already decided",
                "rationale": "Already done",
            })

        assert response.status_code == 200
        data = response.json()
        assert data.get("idempotent") is True


class TestBoardMCPTools:
    """Tests for get_board_items and update_board_item MCP tools."""

    @pytest.mark.asyncio
    async def test_get_board_items_returns_formatted_list(self, tmp_path):
        """get_board_items returns formatted list."""
        import smm_sync.mcp_server as mcp_mod
        import json

        smm_dir = tmp_path / ".smm"
        smm_dir.mkdir()
        board_data = {"items": [
            {"id": "1", "title": "Migrate DB", "status": "backlog", "type": "decision", "description": "Should we?"},
            {"id": "2", "title": "Add tests", "status": "in_progress", "type": "task"},
        ]}
        (smm_dir / "board.json").write_text(json.dumps(board_data))

        original_smm_dir = mcp_mod._smm_dir
        original_context_loaded = mcp_mod._context_loaded
        try:
            mcp_mod._smm_dir = smm_dir
            mcp_mod._context_loaded = True
            result = await mcp_mod.get_board_items(status="all")
        finally:
            mcp_mod._smm_dir = original_smm_dir
            mcp_mod._context_loaded = original_context_loaded

        assert "Migrate DB" in result
        assert "Add tests" in result

    @pytest.mark.asyncio
    async def test_update_board_item_is_idempotent(self, tmp_path):
        """update_board_item is idempotent (same status twice = no error)."""
        import smm_sync.mcp_server as mcp_mod
        import json

        smm_dir = tmp_path / ".smm"
        smm_dir.mkdir()
        item_id = str(uuid.uuid4())
        board_data = {"items": [{"id": item_id, "title": "Test", "status": "backlog", "type": "task"}]}
        (smm_dir / "board.json").write_text(json.dumps(board_data))

        original_smm_dir = mcp_mod._smm_dir
        original_context_loaded = mcp_mod._context_loaded
        try:
            mcp_mod._smm_dir = smm_dir
            mcp_mod._context_loaded = True
            r1 = await mcp_mod.update_board_item(item_id=item_id, status="in_progress")
            r2 = await mcp_mod.update_board_item(item_id=item_id, status="in_progress")
        finally:
            mcp_mod._smm_dir = original_smm_dir
            mcp_mod._context_loaded = original_context_loaded

        assert r1["success"] is True
        assert r2["success"] is True
