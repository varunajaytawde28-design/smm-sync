"""Tests for smm_sync.digest module.

All tests mock file reads — no real API calls.
"""
from __future__ import annotations

import asyncio
import json
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import pytest

from smm_sync.digest import (
    DigestData,
    format_slack,
    format_terminal,
    generate_digest,
    post_to_slack,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_smm_dir(tmp_path: Path, injection_entries: list[dict] | None = None) -> Path:
    """Create a minimal .smm/ dir with optional compliance log."""
    smm = tmp_path / ".smm"
    smm.mkdir()
    if injection_entries is not None:
        log = smm / "compliance_lineage.jsonl"
        with open(log, "w") as f:
            for e in injection_entries:
                f.write(json.dumps(e) + "\n")
    return smm


def _injection(agent: str = "claude-code", minutes_ago: int = 10) -> dict:
    ts = (datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)).isoformat()
    return {
        "timestamp": ts,
        "event_type": "context_injection",
        "agent": agent,
        "tool_name": "query_decisions",
        "session_id": "test:123",
    }


# ---------------------------------------------------------------------------
# test_generate_digest_returns_data_object
# ---------------------------------------------------------------------------

def test_generate_digest_returns_data_object(tmp_path):
    """generate_digest returns a DigestData instance."""
    smm = _make_smm_dir(tmp_path, [_injection()])
    data = asyncio.run(generate_digest(smm, None, "week"))
    assert isinstance(data, DigestData)


# ---------------------------------------------------------------------------
# test_format_terminal_contains_key_sections
# ---------------------------------------------------------------------------

def test_format_terminal_contains_key_sections(tmp_path):
    """format_terminal output contains all expected section headers."""
    smm = _make_smm_dir(tmp_path, [_injection(), _injection(agent="cursor")])
    data = asyncio.run(generate_digest(smm, None, "week"))
    output = format_terminal(data)

    assert "CAPTURED THIS PERIOD" in output
    assert "AGENT ACTIVITY" in output
    assert "GRAPH HEALTH" in output
    assert "Est. time saved" in output
    assert "smm query" in output


# ---------------------------------------------------------------------------
# test_format_terminal_no_crash_on_zero_injections
# ---------------------------------------------------------------------------

def test_format_terminal_no_crash_on_zero_injections(tmp_path):
    """format_terminal works with no injection data (empty log)."""
    smm = _make_smm_dir(tmp_path, [])
    data = asyncio.run(generate_digest(smm, None, "week"))
    output = format_terminal(data)

    assert isinstance(output, str)
    assert len(output) > 0
    assert "0 context injections total" in output


# ---------------------------------------------------------------------------
# test_format_slack_returns_valid_block_kit
# ---------------------------------------------------------------------------

def test_format_slack_returns_valid_block_kit(tmp_path):
    """format_slack returns a dict with 'blocks' key (Slack Block Kit)."""
    smm = _make_smm_dir(tmp_path, [_injection()])
    data = asyncio.run(generate_digest(smm, None, "week"))
    payload = format_slack(data)

    assert isinstance(payload, dict)
    assert "blocks" in payload
    assert isinstance(payload["blocks"], list)
    assert len(payload["blocks"]) > 0
    # First block must be a header
    assert payload["blocks"][0]["type"] == "header"


# ---------------------------------------------------------------------------
# test_post_to_slack_never_raises_on_bad_url
# ---------------------------------------------------------------------------

def test_post_to_slack_never_raises_on_bad_url(tmp_path):
    """post_to_slack silently handles network errors — never raises."""
    smm = _make_smm_dir(tmp_path, [])
    data = asyncio.run(generate_digest(smm, None, "week"))

    # Should not raise — bad URL just prints a warning to stderr
    asyncio.run(post_to_slack("http://localhost:1/bad-webhook", data))


# ---------------------------------------------------------------------------
# test_digest_period_day_sets_correct_cutoff
# ---------------------------------------------------------------------------

def test_digest_period_day_sets_correct_cutoff(tmp_path):
    """period='day' only counts injections from the last 24 hours."""
    now = datetime.now(timezone.utc)
    old_entry = {
        "timestamp": (now - timedelta(hours=48)).isoformat(),
        "event_type": "context_injection",
        "agent": "claude-code",
        "tool_name": "query_decisions",
        "session_id": "test:1",
    }
    recent_entry = {
        "timestamp": (now - timedelta(hours=1)).isoformat(),
        "event_type": "context_injection",
        "agent": "claude-code",
        "tool_name": "query_decisions",
        "session_id": "test:2",
    }
    smm = _make_smm_dir(tmp_path, [old_entry, recent_entry])
    data = asyncio.run(generate_digest(smm, None, "day"))

    # Only the recent entry should be counted
    assert data.total_injections == 1
    assert data.period_label == "Last 24 hours"


# ---------------------------------------------------------------------------
# test_digest_period_week_sets_correct_cutoff
# ---------------------------------------------------------------------------

def test_digest_period_week_sets_correct_cutoff(tmp_path):
    """period='week' counts injections from the last 7 days."""
    now = datetime.now(timezone.utc)
    old_entry = {
        "timestamp": (now - timedelta(days=10)).isoformat(),
        "event_type": "context_injection",
        "agent": "claude-code",
        "tool_name": "query_decisions",
        "session_id": "test:1",
    }
    recent_entry = {
        "timestamp": (now - timedelta(days=3)).isoformat(),
        "event_type": "context_injection",
        "agent": "cursor",
        "tool_name": "query_decisions",
        "session_id": "test:2",
    }
    smm = _make_smm_dir(tmp_path, [old_entry, recent_entry])
    data = asyncio.run(generate_digest(smm, None, "week"))

    assert data.total_injections == 1
    assert "Week of" in data.period_label


# ---------------------------------------------------------------------------
# test_digest_period_month_sets_correct_cutoff
# ---------------------------------------------------------------------------

def test_digest_period_month_sets_correct_cutoff(tmp_path):
    """period='month' counts injections from the last 30 days."""
    now = datetime.now(timezone.utc)
    entries = [
        {
            "timestamp": (now - timedelta(days=25)).isoformat(),
            "event_type": "context_injection",
            "agent": "claude-code",
            "tool_name": "query_decisions",
            "session_id": "test:1",
        },
        {
            "timestamp": (now - timedelta(days=35)).isoformat(),
            "event_type": "context_injection",
            "agent": "claude-code",
            "tool_name": "query_decisions",
            "session_id": "test:2",
        },
    ]
    smm = _make_smm_dir(tmp_path, entries)
    data = asyncio.run(generate_digest(smm, None, "month"))

    assert data.total_injections == 1
    assert data.period_label == "Last 30 days"
