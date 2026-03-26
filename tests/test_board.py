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
    """Create a temp .smm directory with empty board.json."""
    smm_dir = tmp_path / ".smm"
    smm_dir.mkdir()
    board = {"items": []}
    (smm_dir / "board.json").write_text(json.dumps(board))
    return smm_dir


class TestBoardCRUD:
    """Full CRUD tests for the board API."""

    def test_get_empty_board(self, client, board_dir):
        """GET /api/board returns empty grouped dict when board is empty."""
        with patch("smm_sync.dashboard.app._get_smm_dir", return_value=board_dir):
            response = client.get("/api/board")
        assert response.status_code == 200
        data = response.json()
        assert data["items"] == []
        assert "backlog" in data["grouped"]

    def test_create_and_retrieve_item(self, client, board_dir):
        """POST + GET correctly stores and retrieves an item."""
        with patch("smm_sync.dashboard.app._get_smm_dir", return_value=board_dir):
            create_resp = client.post("/api/board", json={
                "title": "Migrate DB",
                "type": "decision",
                "description": "Should we migrate?",
                "priority": "high",
            })
            assert create_resp.status_code == 200
            item_id = create_resp.json()["item"]["id"]

            get_resp = client.get("/api/board")
            assert get_resp.status_code == 200
            items = get_resp.json()["items"]
            assert any(i["id"] == item_id for i in items)

    def test_update_item_status_idempotent(self, client, board_dir):
        """PATCH with same status twice is idempotent (no error)."""
        item_id = str(uuid.uuid4())
        board_data = {"items": [{"id": item_id, "title": "Test", "status": "backlog", "type": "task"}]}
        (board_dir / "board.json").write_text(json.dumps(board_data))

        with patch("smm_sync.dashboard.app._get_smm_dir", return_value=board_dir):
            r1 = client.patch(f"/api/board/{item_id}", json={"status": "in_progress"})
            r2 = client.patch(f"/api/board/{item_id}", json={"status": "in_progress"})

        assert r1.status_code == 200
        assert r2.status_code == 200

    def test_resolve_board_item_creates_graph_decision(self, client, board_dir):
        """POST /api/board/{id}/resolve calls add_decision and marks done."""
        item_id = str(uuid.uuid4())
        board_data = {"items": [{"id": item_id, "title": "Migration question", "status": "backlog", "type": "decision", "created_by": "varun"}]}
        (board_dir / "board.json").write_text(json.dumps(board_data))

        with patch("smm_sync.dashboard.app._get_smm_dir", return_value=board_dir):
            with patch("smm_sync.context_graph.client.GraphClient.add_decision", new_callable=AsyncMock) as mock_add:
                mock_add.return_value = "decision-uuid-123"
                response = client.post(f"/api/board/{item_id}/resolve", json={
                    "decision": "We will migrate to FalkorDB",
                    "rationale": "Docker now available",
                    "alternatives": ["Stay on Kuzu"],
                })

        assert response.status_code == 200
        data = response.json()
        assert data["success"] is True
        assert "decision_id" in data

    def test_resolve_board_item_idempotent(self, client, board_dir):
        """POST /api/board/{id}/resolve twice returns idempotent=True on second call."""
        item_id = str(uuid.uuid4())
        board_data = {"items": [{"id": item_id, "title": "Migration", "status": "done", "type": "decision", "linked_decision_id": "existing-id", "created_by": "varun"}]}
        (board_dir / "board.json").write_text(json.dumps(board_data))

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
        try:
            mcp_mod._smm_dir = smm_dir
            result = await mcp_mod.get_board_items(status="all")
        finally:
            mcp_mod._smm_dir = original_smm_dir

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
        try:
            mcp_mod._smm_dir = smm_dir
            r1 = await mcp_mod.update_board_item(item_id=item_id, status="in_progress")
            r2 = await mcp_mod.update_board_item(item_id=item_id, status="in_progress")
        finally:
            mcp_mod._smm_dir = original_smm_dir

        assert r1["success"] is True
        assert r2["success"] is True
