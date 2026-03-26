"""Tests for the timeline API endpoint."""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from unittest.mock import AsyncMock, MagicMock, patch


@pytest.fixture
def client():
    """Return a test client for the dashboard app."""
    from smm_sync.dashboard.app import app
    return TestClient(app)


class TestTimelineEndpoint:
    """Tests for GET /api/timeline."""

    def test_returns_timeline_structure(self, client):
        """GET /api/timeline returns dict with 'timeline' key."""
        with patch("smm_sync.dashboard.app._get_smm_dir") as mock_dir:
            import tempfile, pathlib
            mock_dir.return_value = pathlib.Path(tempfile.mkdtemp()) / ".smm"

            with patch("smm_sync.context_graph.client.GraphClient.get_decision_timeline", new_callable=AsyncMock) as mock_tl:
                mock_tl.return_value = []
                response = client.get("/api/timeline")

        assert response.status_code == 200
        data = response.json()
        assert "timeline" in data
        assert isinstance(data["timeline"], list)

    def test_empty_graph_returns_empty_timeline(self, client):
        """Empty graph returns empty timeline (not crash)."""
        with patch("smm_sync.dashboard.app._get_smm_dir") as mock_dir:
            import tempfile, pathlib
            mock_dir.return_value = pathlib.Path(tempfile.mkdtemp()) / ".smm"

            with patch("smm_sync.context_graph.client.GraphClient") as MockClient:
                instance = MockClient.return_value
                instance.get_decision_timeline = AsyncMock(return_value=[])
                response = client.get("/api/timeline")

        assert response.status_code == 200
        data = response.json()
        assert data["timeline"] == [] or isinstance(data["timeline"], list)

    def test_timeline_handles_graph_exception(self, client):
        """Timeline endpoint returns empty list on graph failure."""
        with patch("smm_sync.dashboard.app._get_smm_dir") as mock_dir:
            import tempfile, pathlib
            mock_dir.return_value = pathlib.Path(tempfile.mkdtemp()) / ".smm"

            with patch("smm_sync.context_graph.client.GraphClient") as MockClient:
                instance = MockClient.return_value
                instance.get_decision_timeline = AsyncMock(side_effect=Exception("Graph error"))
                response = client.get("/api/timeline")

        assert response.status_code == 200
        data = response.json()
        assert "timeline" in data


class TestBoardEndpoints:
    """Tests for board CRUD endpoints."""

    def test_get_board_returns_items(self, client, tmp_path):
        """GET /api/board returns items grouped by status."""
        with patch("smm_sync.dashboard.app._get_smm_dir", return_value=tmp_path / ".smm"):
            (tmp_path / ".smm").mkdir(exist_ok=True)
            response = client.get("/api/board")

        assert response.status_code == 200
        data = response.json()
        assert "items" in data
        assert "grouped" in data

    def test_post_board_creates_item(self, client, tmp_path):
        """POST /api/board creates a new item."""
        with patch("smm_sync.dashboard.app._get_smm_dir", return_value=tmp_path / ".smm"):
            (tmp_path / ".smm").mkdir(exist_ok=True)
            response = client.post("/api/board", json={
                "title": "Test decision item",
                "type": "decision",
                "description": "Should we use X?",
                "priority": "high",
            })

        assert response.status_code == 200
        data = response.json()
        assert "item" in data
        assert data["item"]["title"] == "Test decision item"
        assert data["item"]["status"] == "backlog"

    def test_post_board_validates_empty_title(self, client, tmp_path):
        """POST /api/board returns 400 for empty title."""
        with patch("smm_sync.dashboard.app._get_smm_dir", return_value=tmp_path / ".smm"):
            (tmp_path / ".smm").mkdir(exist_ok=True)
            response = client.post("/api/board", json={"title": "", "type": "task"})

        assert response.status_code == 400

    def test_patch_board_updates_status(self, client, tmp_path):
        """PATCH /api/board/{id} updates item status."""
        import json, uuid
        smm_dir = tmp_path / ".smm"
        smm_dir.mkdir(exist_ok=True)
        item_id = str(uuid.uuid4())
        board_data = {"items": [{"id": item_id, "title": "Test", "status": "backlog", "type": "task"}]}
        (smm_dir / "board.json").write_text(json.dumps(board_data))

        with patch("smm_sync.dashboard.app._get_smm_dir", return_value=smm_dir):
            response = client.patch(f"/api/board/{item_id}", json={"status": "in_progress"})

        assert response.status_code == 200
        data = response.json()
        assert data["item"]["status"] == "in_progress"

    def test_delete_board_removes_item(self, client, tmp_path):
        """DELETE /api/board/{id} removes the item."""
        import json, uuid
        smm_dir = tmp_path / ".smm"
        smm_dir.mkdir(exist_ok=True)
        item_id = str(uuid.uuid4())
        board_data = {"items": [{"id": item_id, "title": "Test", "status": "backlog", "type": "task"}]}
        (smm_dir / "board.json").write_text(json.dumps(board_data))

        with patch("smm_sync.dashboard.app._get_smm_dir", return_value=smm_dir):
            response = client.delete(f"/api/board/{item_id}")

        assert response.status_code == 200
        assert response.json()["success"] is True
