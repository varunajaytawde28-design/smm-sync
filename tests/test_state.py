"""Tests for smm_sync.state — propose-validate-commit event log."""
import pytest
from pathlib import Path

from smm_sync import state


@pytest.fixture
def smm_dir(tmp_path):
    d = tmp_path / ".smm"
    d.mkdir()
    return d


# ---------------------------------------------------------------------------
# Event log basics
# ---------------------------------------------------------------------------

def test_events_jsonl_created_on_first_propose(smm_dir):
    state.propose(smm_dir, "session_started", "s1", {})
    assert (smm_dir / "events.jsonl").exists()


def test_events_jsonl_is_append_only(smm_dir):
    state.propose(smm_dir, "session_started", "s1", {})
    state.propose(smm_dir, "session_started", "s2", {})
    events = state.read_events(smm_dir)
    assert len(events) == 2


def test_each_event_has_required_fields(smm_dir):
    result = state.propose(smm_dir, "session_started", "s1", {})
    events = state.read_events(smm_dir)
    assert len(events) == 1
    e = events[0]
    assert "event_id" in e
    assert e["event_id"].startswith("evt_")
    assert "event_type" in e
    assert "session_id" in e
    assert "timestamp" in e
    assert "status" in e


def test_state_json_is_human_readable(smm_dir):
    state.propose(smm_dir, "session_started", "s1", {})
    raw = (smm_dir / "state.json").read_text()
    assert "\n" in raw


# ---------------------------------------------------------------------------
# session_started / session_ended
# ---------------------------------------------------------------------------

def test_session_started_always_accepted(smm_dir):
    result = state.propose(smm_dir, "session_started", "s1", {})
    assert result["accepted"] is True


def test_session_ended_accepted(smm_dir):
    state.propose(smm_dir, "session_started", "s1", {})
    result = state.propose(smm_dir, "session_ended", "s1", {})
    assert result["accepted"] is True


def test_session_ended_releases_files(smm_dir):
    state.propose(smm_dir, "session_started", "s1", {})
    state.propose(smm_dir, "file_claimed", "s1", {"filepath": "auth.py"})
    state.propose(smm_dir, "session_ended", "s1", {})
    current = state.get_current_state(smm_dir)
    assert "auth.py" not in current["claimed_files"]


# ---------------------------------------------------------------------------
# file_claimed
# ---------------------------------------------------------------------------

def test_file_claimed_accepted(smm_dir):
    result = state.propose(smm_dir, "file_claimed", "s1", {"filepath": "auth.py"})
    assert result["accepted"] is True


def test_file_claimed_rejected_if_already_claimed_by_other(smm_dir):
    state.propose(smm_dir, "file_claimed", "s1", {"filepath": "auth.py"})
    result = state.propose(smm_dir, "file_claimed", "s2", {"filepath": "auth.py"})
    assert result["accepted"] is False
    assert "s1" in result["reason"]


def test_file_claimed_accepted_by_same_session(smm_dir):
    state.propose(smm_dir, "file_claimed", "s1", {"filepath": "auth.py"})
    result = state.propose(smm_dir, "file_claimed", "s1", {"filepath": "auth.py"})
    assert result["accepted"] is True


def test_rejected_event_still_recorded_in_log(smm_dir):
    state.propose(smm_dir, "file_claimed", "s1", {"filepath": "auth.py"})
    state.propose(smm_dir, "file_claimed", "s2", {"filepath": "auth.py"})
    events = state.read_events(smm_dir)
    rejected = [e for e in events if e["status"] == "rejected"]
    assert len(rejected) == 1


# ---------------------------------------------------------------------------
# file_released
# ---------------------------------------------------------------------------

def test_file_released_accepted(smm_dir):
    state.propose(smm_dir, "file_claimed", "s1", {"filepath": "auth.py"})
    result = state.propose(smm_dir, "file_released", "s1", {"filepath": "auth.py"})
    assert result["accepted"] is True


def test_file_released_rejected_if_not_claimed(smm_dir):
    result = state.propose(smm_dir, "file_released", "s1", {"filepath": "auth.py"})
    assert result["accepted"] is False


def test_file_released_rejected_if_owned_by_other(smm_dir):
    state.propose(smm_dir, "file_claimed", "s1", {"filepath": "auth.py"})
    result = state.propose(smm_dir, "file_released", "s2", {"filepath": "auth.py"})
    assert result["accepted"] is False


def test_after_release_file_can_be_reclaimed(smm_dir):
    state.propose(smm_dir, "file_claimed", "s1", {"filepath": "auth.py"})
    state.propose(smm_dir, "file_released", "s1", {"filepath": "auth.py"})
    result = state.propose(smm_dir, "file_claimed", "s2", {"filepath": "auth.py"})
    assert result["accepted"] is True


# ---------------------------------------------------------------------------
# context_refreshed
# ---------------------------------------------------------------------------

def test_context_refreshed_accepted_when_hash_changes(smm_dir):
    result = state.propose(smm_dir, "context_refreshed", "s1", {"context_hash": "abc123"})
    assert result["accepted"] is True


def test_context_refreshed_rejected_when_hash_same(smm_dir):
    state.propose(smm_dir, "context_refreshed", "s1", {"context_hash": "abc123"})
    result = state.propose(smm_dir, "context_refreshed", "s1", {"context_hash": "abc123"})
    assert result["accepted"] is False


def test_context_refreshed_accepted_when_hash_differs(smm_dir):
    state.propose(smm_dir, "context_refreshed", "s1", {"context_hash": "abc123"})
    result = state.propose(smm_dir, "context_refreshed", "s1", {"context_hash": "def456"})
    assert result["accepted"] is True


# ---------------------------------------------------------------------------
# materialize_state
# ---------------------------------------------------------------------------

def test_materialize_empty_events(smm_dir):
    s = state.materialize_state([])
    assert s["claimed_files"] == {}
    assert s["active_sessions"] == {}
    assert s["last_refresh"] == ""


def test_materialize_claimed_files(smm_dir):
    state.propose(smm_dir, "file_claimed", "s1", {"filepath": "auth.py", "task": "refactor"})
    s = state.get_current_state(smm_dir)
    assert "auth.py" in s["claimed_files"]
    assert s["claimed_files"]["auth.py"]["session_id"] == "s1"
    assert s["claimed_files"]["auth.py"]["task"] == "refactor"
