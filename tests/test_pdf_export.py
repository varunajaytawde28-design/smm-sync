"""Tests for PDF export endpoints."""
from __future__ import annotations
import json
from pathlib import Path
from unittest.mock import patch, MagicMock
import pytest
from fastapi.testclient import TestClient
from smm_sync.dashboard.app import app

client = TestClient(app, raise_server_exceptions=True)


def _make_smm(tmp_path):
    smm = tmp_path / ".smm"
    smm.mkdir()
    return smm


def test_decisions_pdf_endpoint_returns_200(tmp_path):
    """GET /api/decisions/export/pdf returns 200."""
    smm = _make_smm(tmp_path)
    with patch("smm_sync.dashboard.app._get_smm_dir", return_value=smm), \
         patch("smm_sync.dashboard.app._get_graph_client", return_value=None):
        r = client.get("/api/decisions/export/pdf")
    assert r.status_code == 200


def test_decisions_pdf_content_type_is_pdf(tmp_path):
    """GET /api/decisions/export/pdf returns application/pdf content type."""
    smm = _make_smm(tmp_path)
    with patch("smm_sync.dashboard.app._get_smm_dir", return_value=smm), \
         patch("smm_sync.dashboard.app._get_graph_client", return_value=None):
        r = client.get("/api/decisions/export/pdf")
    assert "pdf" in r.headers.get("content-type", "").lower()


def test_compliance_pdf_endpoint_returns_200(tmp_path):
    """GET /api/compliance/export/pdf returns 200."""
    smm = _make_smm(tmp_path)
    with patch("smm_sync.dashboard.app._get_smm_dir", return_value=smm):
        r = client.get("/api/compliance/export/pdf")
    assert r.status_code == 200


def test_pdf_handles_empty_graph_gracefully(tmp_path):
    """PDF export with empty .smm/ dir returns valid PDF."""
    smm = _make_smm(tmp_path)
    with patch("smm_sync.dashboard.app._get_smm_dir", return_value=smm), \
         patch("smm_sync.dashboard.app._get_graph_client", return_value=None):
        r = client.get("/api/decisions/export/pdf")
    assert r.status_code == 200
    # PDF starts with %PDF
    assert r.content[:4] == b"%PDF"


def test_compliance_pdf_has_pdf_magic_bytes(tmp_path):
    """GET /api/compliance/export/pdf returns content starting with %PDF."""
    smm = _make_smm(tmp_path)
    with patch("smm_sync.dashboard.app._get_smm_dir", return_value=smm):
        r = client.get("/api/compliance/export/pdf")
    assert r.status_code == 200
    assert r.content[:4] == b"%PDF"


def test_decisions_pdf_with_compliance_log(tmp_path):
    """PDF export with decision entries in compliance log generates valid PDF."""
    smm = _make_smm(tmp_path)
    log = smm / "compliance_lineage.jsonl"
    log.write_text(
        json.dumps({
            "event_type": "decision_added",
            "decision_title": "Test Decision",
            "confidence": 0.9,
            "timestamp": "2026-01-01T12:00:00Z",
        }) + "\n"
    )
    with patch("smm_sync.dashboard.app._get_smm_dir", return_value=smm), \
         patch("smm_sync.dashboard.app._get_graph_client", return_value=None):
        r = client.get("/api/decisions/export/pdf")
    assert r.status_code == 200
    assert r.content[:4] == b"%PDF"


def test_compliance_pdf_content_disposition(tmp_path):
    """GET /api/compliance/export/pdf has correct Content-Disposition header."""
    smm = _make_smm(tmp_path)
    with patch("smm_sync.dashboard.app._get_smm_dir", return_value=smm):
        r = client.get("/api/compliance/export/pdf")
    assert r.status_code == 200
    cd = r.headers.get("content-disposition", "")
    assert "attachment" in cd
    assert ".pdf" in cd


def test_decisions_pdf_content_disposition(tmp_path):
    """GET /api/decisions/export/pdf has correct Content-Disposition header."""
    smm = _make_smm(tmp_path)
    with patch("smm_sync.dashboard.app._get_smm_dir", return_value=smm), \
         patch("smm_sync.dashboard.app._get_graph_client", return_value=None):
        r = client.get("/api/decisions/export/pdf")
    assert r.status_code == 200
    cd = r.headers.get("content-disposition", "")
    assert "attachment" in cd
    assert ".pdf" in cd
