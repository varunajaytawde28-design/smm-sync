"""Tests for compliance lineage logging.

Research basis: EU AI Act (Aug 2026), SOC 2 AI governance controls.
The lineage log is an append-only audit trail of what AI agents knew and when.

No real API calls. Uses tmp_path for file I/O.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from smm_sync.compliance.lineage import LineageLogger


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture()
def log_path(tmp_path) -> Path:
    return tmp_path / "compliance_lineage.jsonl"


@pytest.fixture()
def logger(log_path) -> LineageLogger:
    return LineageLogger(log_path)


# ---------------------------------------------------------------------------
# log_context_injection
# ---------------------------------------------------------------------------

class TestLogContextInjection:
    def test_log_entry_written_on_injection(self, logger, log_path):
        """A log entry must be written to the JSONL file on injection."""
        entry_id = logger.log_context_injection(
            query="why did we choose Kuzu?",
            decisions_surfaced=["Use Kuzu as embedded DB", "No Docker on macOS"],
            agent="claude-code",
            session_id="test-session-1",
            tool_name="query_decisions",
        )

        assert log_path.exists()
        lines = [l for l in log_path.read_text().splitlines() if l.strip()]
        assert len(lines) == 1
        entry = json.loads(lines[0])
        assert entry["entry_id"] == entry_id
        assert entry["event_type"] == "context_injection"
        assert entry["agent"] == "claude-code"
        assert entry["tool_name"] == "query_decisions"
        assert "Use Kuzu as embedded DB" in entry["decisions_surfaced"]
        assert entry["decision_count"] == 2

    def test_log_entry_written_on_decision_added(self, logger, log_path):
        """A log entry must be written when a decision is added."""
        entry_id = logger.log_decision_added(
            decision_title="Use os.rename() for locking",
            source_type="github_pr",
            project="smm-sync",
            confidence=0.90,
            contradictions_found=["Old locking decision"],
        )

        assert log_path.exists()
        lines = [l for l in log_path.read_text().splitlines() if l.strip()]
        assert len(lines) == 1
        entry = json.loads(lines[0])
        assert entry["entry_id"] == entry_id
        assert entry["event_type"] == "decision_added"
        assert entry["confidence"] == 0.90
        assert "Old locking decision" in entry["contradictions_found"]

    def test_query_truncated_to_500_chars(self, logger, log_path):
        """Long queries must be truncated to 500 chars in the log."""
        long_query = "x" * 1000
        logger.log_context_injection(
            query=long_query,
            decisions_surfaced=[],
            agent="test",
        )
        entry = json.loads(log_path.read_text().strip())
        assert len(entry["query"]) <= 500

    def test_returns_uuid_string(self, logger):
        """log_context_injection must return a non-empty UUID string."""
        entry_id = logger.log_context_injection(
            query="test",
            decisions_surfaced=["D1"],
            agent="test-agent",
        )
        assert isinstance(entry_id, str)
        assert len(entry_id) > 0
        # Should be UUID format
        import uuid
        uuid.UUID(entry_id)  # raises if not valid UUID


# ---------------------------------------------------------------------------
# Append-only guarantee
# ---------------------------------------------------------------------------

class TestAppendOnly:
    def test_log_file_is_append_only(self, logger, log_path):
        """Existing entries must be preserved across multiple calls."""
        logger.log_context_injection(
            query="first query",
            decisions_surfaced=["Decision A"],
            agent="agent1",
        )
        logger.log_context_injection(
            query="second query",
            decisions_surfaced=["Decision B"],
            agent="agent2",
        )

        lines = [l for l in log_path.read_text().splitlines() if l.strip()]
        assert len(lines) == 2

        entries = [json.loads(l) for l in lines]
        queries = [e["query"] for e in entries]
        assert "first query" in queries
        assert "second query" in queries

    def test_multiple_decision_added_entries(self, logger, log_path):
        """Multiple decision_added entries accumulate in the log."""
        for i in range(3):
            logger.log_decision_added(
                decision_title=f"Decision {i}",
                source_type="manual",
                project="test",
                confidence=0.9,
                contradictions_found=[],
            )

        lines = [l for l in log_path.read_text().splitlines() if l.strip()]
        assert len(lines) == 3


# ---------------------------------------------------------------------------
# get_session_lineage
# ---------------------------------------------------------------------------

class TestGetSessionLineage:
    def test_returns_correct_entries_for_session(self, logger):
        """get_session_lineage must return only entries for the given session."""
        logger.log_context_injection(
            query="q1",
            decisions_surfaced=["D1"],
            agent="agent",
            session_id="session-A",
        )
        logger.log_context_injection(
            query="q2",
            decisions_surfaced=["D2"],
            agent="agent",
            session_id="session-B",
        )
        logger.log_context_injection(
            query="q3",
            decisions_surfaced=["D3"],
            agent="agent",
            session_id="session-A",
        )

        entries = logger.get_session_lineage("session-A")
        assert len(entries) == 2
        queries = [e["query"] for e in entries]
        assert "q1" in queries
        assert "q3" in queries
        assert "q2" not in queries

    def test_returns_empty_for_unknown_session(self, logger):
        logger.log_context_injection(
            query="q",
            decisions_surfaced=[],
            agent="agent",
            session_id="other-session",
        )
        entries = logger.get_session_lineage("nonexistent-session")
        assert entries == []

    def test_returns_empty_when_log_missing(self, tmp_path):
        """get_session_lineage must return [] when log file doesn't exist."""
        logger = LineageLogger(tmp_path / "nonexistent.jsonl")
        entries = logger.get_session_lineage("any-session")
        assert entries == []


# ---------------------------------------------------------------------------
# get_decision_lineage
# ---------------------------------------------------------------------------

class TestGetDecisionLineage:
    def test_returns_entries_where_decision_surfaced(self, logger):
        logger.log_context_injection(
            query="q1",
            decisions_surfaced=["Target Decision", "Other Decision"],
            agent="agent",
        )
        logger.log_context_injection(
            query="q2",
            decisions_surfaced=["Unrelated Decision"],
            agent="agent",
        )
        logger.log_context_injection(
            query="q3",
            decisions_surfaced=["Target Decision"],
            agent="agent",
        )

        entries = logger.get_decision_lineage("Target Decision")
        assert len(entries) == 2
        queries = [e["query"] for e in entries]
        assert "q1" in queries
        assert "q3" in queries

    def test_returns_empty_for_unknown_decision(self, logger):
        logger.log_context_injection(
            query="q",
            decisions_surfaced=["Known Decision"],
            agent="agent",
        )
        entries = logger.get_decision_lineage("Unknown Decision")
        assert entries == []


# ---------------------------------------------------------------------------
# Silent failure
# ---------------------------------------------------------------------------

class TestSilentFailure:
    def test_logger_never_raises_on_filesystem_error(self, tmp_path):
        """Logger must never raise — silent failure on any error."""
        # Use an unwritable path
        bad_path = tmp_path / "nonexistent_dir" / "sub" / "lineage.jsonl"
        # Make the parent a FILE so we can't create a dir there
        blocker = tmp_path / "nonexistent_dir"
        blocker.write_text("I am a file, not a dir")

        # LineageLogger.__init__ tries mkdir, which will fail or succeed
        # Either way, log_context_injection must not raise
        try:
            logger = LineageLogger(bad_path)
            # This should not raise even if write fails
            entry_id = logger.log_context_injection(
                query="test",
                decisions_surfaced=[],
                agent="test",
            )
            assert isinstance(entry_id, str)
        except Exception as e:
            pytest.fail(f"LineageLogger raised unexpectedly: {e}")

    def test_get_session_lineage_returns_empty_on_corrupt_line(self, logger, log_path):
        """get_session_lineage must handle corrupt JSONL lines gracefully."""
        log_path.write_text("not valid json\n", encoding="utf-8")
        # Should return [] without raising
        entries = logger.get_session_lineage("any")
        assert entries == []


# ---------------------------------------------------------------------------
# get_stats
# ---------------------------------------------------------------------------

class TestGetStats:
    def test_stats_on_empty_log(self, logger):
        stats = logger.get_stats()
        assert stats["total_injections"] == 0
        assert stats["unique_decisions"] == 0
        assert stats["sessions"] == 0

    def test_stats_counts_correctly(self, logger):
        logger.log_context_injection(
            query="q1",
            decisions_surfaced=["D1", "D2"],
            agent="agent",
            session_id="s1",
        )
        logger.log_context_injection(
            query="q2",
            decisions_surfaced=["D1", "D3"],
            agent="agent",
            session_id="s2",
        )
        stats = logger.get_stats()
        assert stats["total_injections"] == 2
        assert stats["unique_decisions"] == 3  # D1, D2, D3
        assert stats["sessions"] == 2

    def test_most_surfaced_ordered_by_count(self, logger):
        for _ in range(5):
            logger.log_context_injection(
                query="q", decisions_surfaced=["D1"], agent="agent"
            )
        for _ in range(2):
            logger.log_context_injection(
                query="q", decisions_surfaced=["D2"], agent="agent"
            )

        stats = logger.get_stats()
        assert stats["most_surfaced"][0]["title"] == "D1"
        assert stats["most_surfaced"][0]["count"] == 5
