"""
Compliance Lineage Logger for CaaS.

Every time context is injected into an AI agent session via MCP,
this module writes an append-only log entry recording exactly:
- What decisions were surfaced
- Which AI agent received them
- When the injection occurred
- What query triggered the context retrieval

This is the AI Decision Audit Trail required by:
- EU AI Act (high-risk system requirements, Aug 2026)
- SOC 2 AI governance controls
- Enterprise security review requirements

The log is append-only. Entries are never deleted or modified.
This is the legal record of what your AI agents knew and when.

Log location: .smm/compliance_lineage.jsonl
Format: newline-delimited JSON (JSONL) — one entry per line
"""
from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from smm_sync.security import DEBUG_MODE


class LineageLogger:
    """Append-only compliance lineage logger.

    Thread-safe via atomic appends (OS-level file append is atomic for small writes).
    Never raises exceptions — logging failure must never block context delivery.

    Args:
        log_path: Path to the JSONL log file (.smm/compliance_lineage.jsonl).
    """

    def __init__(self, log_path: Path) -> None:
        self.log_path = log_path
        try:
            self.log_path.parent.mkdir(parents=True, exist_ok=True)
        except Exception as e:
            if DEBUG_MODE:
                raise  # Re-raise in debug mode
            pass  # Never raise in __init__ — silent failure

    def log_context_injection(
        self,
        query: str,
        decisions_surfaced: list[str],
        agent: str,
        session_id: Optional[str] = None,
        tool_name: Optional[str] = None,
    ) -> str:
        """Log a context injection event.

        Called every time an MCP tool returns decisions to an AI agent.
        This is the core audit record: what did the AI know and when?

        Args:
            query: The query or trigger that caused context retrieval.
            decisions_surfaced: List of decision titles that were returned.
            agent: Which AI agent received the context
                   (e.g. "claude-code", "cursor", "mcp-client").
            session_id: Optional session identifier for grouping injections.
            tool_name: Which MCP tool was called (e.g. "query_decisions").

        Returns:
            entry_id: UUID for this log entry (for audit references).
        """
        entry_id = str(uuid.uuid4())
        entry = {
            "entry_id": entry_id,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "event_type": "context_injection",
            "agent": agent,
            "tool_name": tool_name,
            "session_id": session_id,
            "query": query[:500],  # Truncate for storage
            "decisions_surfaced": decisions_surfaced,
            "decision_count": len(decisions_surfaced),
        }

        try:
            with open(self.log_path, "a", encoding="utf-8") as f:
                f.write(json.dumps(entry) + "\n")
        except Exception as e:
            if DEBUG_MODE:
                raise  # Re-raise in debug mode
            pass  # Never block on logging failure

        return entry_id

    def log_decision_added(
        self,
        decision_title: str,
        source_type: str,
        project: str,
        confidence: float,
        contradictions_found: list[str],
    ) -> str:
        """Log when a new decision is added to the graph.

        Args:
            decision_title: Title of the decision being added.
            source_type: Source type key from SOURCE_CONFIDENCE hierarchy.
            project: Project name (graph partition).
            confidence: Confidence score (0.0–1.0) assigned to this decision.
            contradictions_found: Titles of existing decisions that may contradict.

        Returns:
            entry_id: UUID for this log entry.
        """
        entry_id = str(uuid.uuid4())
        entry = {
            "entry_id": entry_id,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "event_type": "decision_added",
            "decision_title": decision_title,
            "source_type": source_type,
            "project": project,
            "confidence": confidence,
            "contradictions_found": contradictions_found,
        }

        try:
            with open(self.log_path, "a", encoding="utf-8") as f:
                f.write(json.dumps(entry) + "\n")
        except Exception as e:
            if DEBUG_MODE:
                raise  # Re-raise in debug mode
            pass

        return entry_id

    def get_session_lineage(self, session_id: str) -> list[dict]:
        """Get all context injections for a specific session.

        Used for audit: "What did the AI agent know in session X?"

        Args:
            session_id: Session identifier to filter by.

        Returns:
            List of log entries for this session, in chronological order.
        """
        entries = []
        try:
            with open(self.log_path, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    entry = json.loads(line)
                    if entry.get("session_id") == session_id:
                        entries.append(entry)
        except Exception:
            pass
        return entries

    def get_decision_lineage(self, decision_title: str) -> list[dict]:
        """Get all injections where a specific decision was surfaced.

        Used for audit: "When was this constraint shown to AI agents?"

        Args:
            decision_title: Title of the decision to search for.

        Returns:
            List of log entries where this decision was surfaced.
        """
        entries = []
        try:
            with open(self.log_path, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    entry = json.loads(line)
                    if decision_title in entry.get("decisions_surfaced", []):
                        entries.append(entry)
        except Exception:
            pass
        return entries

    def get_all_entries(self) -> list[dict]:
        """Return all log entries.

        Args: None

        Returns:
            All log entries in chronological order.
        """
        entries = []
        try:
            with open(self.log_path, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        entries.append(json.loads(line))
                    except Exception:
                        pass
        except Exception:
            pass
        return entries

    def get_stats(self) -> dict:
        """Compute summary statistics over the compliance log.

        Args: None

        Returns:
            Dict with total_injections, unique_decisions, sessions, date_range,
            and most_surfaced (top-5 decisions by injection count).
        """
        entries = self.get_all_entries()
        injection_entries = [e for e in entries if e.get("event_type") == "context_injection"]

        if not injection_entries:
            return {
                "total_injections": 0,
                "unique_decisions": 0,
                "sessions": 0,
                "date_range": None,
                "most_surfaced": [],
            }

        decision_counts: dict[str, int] = {}
        sessions: set[str] = set()
        timestamps = []

        for e in injection_entries:
            for d in e.get("decisions_surfaced", []):
                decision_counts[d] = decision_counts.get(d, 0) + 1
            if e.get("session_id"):
                sessions.add(e["session_id"])
            if e.get("timestamp"):
                timestamps.append(e["timestamp"])

        most_surfaced = sorted(decision_counts.items(), key=lambda x: x[1], reverse=True)[:5]
        date_range = None
        if timestamps:
            timestamps.sort()
            date_range = {"from": timestamps[0][:10], "to": timestamps[-1][:10]}

        return {
            "total_injections": len(injection_entries),
            "unique_decisions": len(decision_counts),
            "sessions": len(sessions),
            "date_range": date_range,
            "most_surfaced": [{"title": t, "count": c} for t, c in most_surfaced],
        }
