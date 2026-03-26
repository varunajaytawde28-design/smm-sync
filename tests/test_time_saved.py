"""Tests for the time saved metric.

All tests mock file reads — no real API calls.
"""
from __future__ import annotations

import json
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from smm_sync.dashboard.app import _calculate_time_saved, app

client = TestClient(app, raise_server_exceptions=True)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_lineage(tmp_path: Path, entries: list[dict]) -> Path:
    """Write compliance_lineage.jsonl and return its path."""
    smm = tmp_path / ".smm"
    smm.mkdir(exist_ok=True)
    log = smm / "compliance_lineage.jsonl"
    with open(log, "w") as f:
        for e in entries:
            f.write(json.dumps(e) + "\n")
    return log


def _injection(minutes_ago: int = 5) -> dict:
    ts = (datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)).isoformat()
    return {
        "timestamp": ts,
        "event_type": "context_injection",
        "agent": "claude-code",
        "tool_name": "query_decisions",
        "session_id": "test:1",
    }


# ---------------------------------------------------------------------------
# test_calculate_time_saved_returns_correct_formula
# ---------------------------------------------------------------------------

def test_calculate_time_saved_returns_correct_formula(tmp_path):
    """Formula: injections × 3.75 minutes."""
    log = _make_lineage(tmp_path, [_injection() for _ in range(4)])
    result = _calculate_time_saved(log)

    # 4 injections in the last 7 days
    assert result["time_saved_minutes_week"] == int(4 * 3.75)
    assert result["baseline_assumption_minutes"] == 15
    assert result["injections_per_session_assumed"] == 4


# ---------------------------------------------------------------------------
# test_calculate_time_saved_zero_on_empty_log
# ---------------------------------------------------------------------------

def test_calculate_time_saved_zero_on_empty_log(tmp_path):
    """Returns zeros when compliance log is empty."""
    log = _make_lineage(tmp_path, [])
    result = _calculate_time_saved(log)

    assert result["time_saved_minutes_week"] == 0
    assert result["time_saved_minutes_today"] == 0
    assert result["time_saved_minutes_total"] == 0


# ---------------------------------------------------------------------------
# test_calculate_time_saved_filters_by_period
# ---------------------------------------------------------------------------

def test_calculate_time_saved_filters_by_period(tmp_path):
    """Only counts injections within the requested period."""
    now = datetime.now(timezone.utc)
    recent = {
        "timestamp": (now - timedelta(days=3)).isoformat(),
        "event_type": "context_injection",
        "agent": "claude-code",
        "tool_name": "query_decisions",
        "session_id": "test:1",
    }
    old = {
        "timestamp": (now - timedelta(days=20)).isoformat(),
        "event_type": "context_injection",
        "agent": "claude-code",
        "tool_name": "query_decisions",
        "session_id": "test:2",
    }
    log = _make_lineage(tmp_path, [recent, old])
    result = _calculate_time_saved(log, period_days=7)

    # Only recent entry falls in 7-day window
    assert result["time_saved_minutes_week"] == int(1 * 3.75)
    # Total counts both
    assert result["time_saved_minutes_total"] == int(2 * 3.75)


# ---------------------------------------------------------------------------
# test_stats_endpoint_includes_time_saved_fields
# ---------------------------------------------------------------------------

def test_stats_endpoint_includes_time_saved_fields(tmp_path):
    """GET /api/stats includes all time_saved fields."""
    smm = tmp_path / ".smm"
    smm.mkdir()
    log = smm / "compliance_lineage.jsonl"
    log.write_text(json.dumps(_injection()) + "\n")

    with patch("smm_sync.dashboard.app._get_smm_dir", return_value=smm):
        r = client.get("/api/stats")

    assert r.status_code == 200
    data = r.json()

    required_keys = [
        "time_saved_minutes_today",
        "time_saved_minutes_week",
        "time_saved_minutes_total",
        "time_saved_formatted_week",
        "time_saved_formatted_total",
        "baseline_assumption_minutes",
        "injections_per_session_assumed",
    ]
    for key in required_keys:
        assert key in data, f"Missing key: {key}"


# ---------------------------------------------------------------------------
# test_time_saved_formatted_correctly
# ---------------------------------------------------------------------------

def test_time_saved_formatted_correctly(tmp_path):
    """time_saved_formatted_week is human-readable (e.g. '3h 5m' or '45m')."""
    # 50 injections × 3.75 = 187.5 → 187 min = 3h 7m
    injections = [_injection(minutes_ago=i * 2) for i in range(50)]
    log = _make_lineage(tmp_path, injections)
    result = _calculate_time_saved(log)

    fmt = result["time_saved_formatted_week"]
    assert isinstance(fmt, str)
    assert len(fmt) > 0
    # Should contain 'h' or 'm'
    assert "h" in fmt or "m" in fmt


# ---------------------------------------------------------------------------
# test_time_saved_appears_in_dashboard_startup
# ---------------------------------------------------------------------------

def test_time_saved_appears_in_dashboard_startup(tmp_path):
    """smm dashboard startup output includes time saved info."""
    from click.testing import CliRunner
    from smm_sync.cli import main

    smm = tmp_path / ".smm"
    smm.mkdir()
    agents_md = tmp_path / "AGENTS.md"
    agents_md.write_text("# AGENTS.md\n\n## Project\n\nTest.\n")

    runner = CliRunner()

    with patch("smm_sync.cli.get_smm_dir", return_value=smm), \
         patch("smm_sync.cli.find_project_root", return_value=tmp_path), \
         patch("smm_sync.dashboard.run_dashboard") as mock_run:
        mock_run.side_effect = SystemExit(0)
        result = runner.invoke(main, ["dashboard"])

    # The output before the SystemExit should include time saved
    assert "time saved" in result.output.lower() or "Est. time saved" in result.output
