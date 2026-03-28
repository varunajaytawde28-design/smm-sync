"""Propose-validate-commit state engine with append-only event log.

Storage:
  .smm/events.jsonl  — append-only event log, never overwritten
  .smm/state.json    — materialized current state, derived from events
"""
from __future__ import annotations

import hashlib
import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

try:
    from filelock import FileLock as _FileLock
    _HAS_FILELOCK = True
except ImportError:
    _HAS_FILELOCK = False

_EVENTS_FILE = "events.jsonl"
_STATE_FILE = "state.json"
_LOCK_FILE = "events.jsonl.lock"

_VALID_EVENT_TYPES = frozenset({
    "file_claimed", "file_released",
    "context_refreshed", "session_started", "session_ended",
})


# ---------------------------------------------------------------------------
# ID generation
# ---------------------------------------------------------------------------

def _generate_event_id() -> str:
    """Generate a unique event ID in format evt_<8char hex>.

    Returns:
        Event ID string like 'evt_a1b2c3d4'.
    """
    return "evt_" + os.urandom(4).hex()


# ---------------------------------------------------------------------------
# Serialisation helpers
# ---------------------------------------------------------------------------

def _now_iso() -> str:
    """Return current UTC time as ISO8601 string.

    Returns:
        ISO8601 timestamp string with UTC timezone.
    """
    return datetime.now(timezone.utc).isoformat()


def _events_path(smm_dir: Path) -> Path:
    return smm_dir / _EVENTS_FILE


def _state_path(smm_dir: Path) -> Path:
    return smm_dir / _STATE_FILE


def _lock_path(smm_dir: Path) -> Path:
    return smm_dir / _LOCK_FILE


# ---------------------------------------------------------------------------
# Event log I/O
# ---------------------------------------------------------------------------

def _read_events(smm_dir: Path) -> list[dict]:
    """Read all events from events.jsonl.

    Args:
        smm_dir: Path to .smm directory.

    Returns:
        List of event dicts in order of insertion.
    """
    path = _events_path(smm_dir)
    if not path.exists():
        return []
    events = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            try:
                events.append(json.loads(line))
            except json.JSONDecodeError:
                pass
    return events


def _append_event(smm_dir: Path, event: dict) -> None:
    """Append a single event to events.jsonl (append-only).

    Args:
        smm_dir: Path to .smm directory.
        event: Event dict to append.
    """
    path = _events_path(smm_dir)
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(event) + "\n")
        f.flush()
        os.fsync(f.fileno())


# ---------------------------------------------------------------------------
# State materialisation
# ---------------------------------------------------------------------------

def materialize_state(events: list[dict]) -> dict:
    """Replay committed events to produce current state.

    Args:
        events: List of event dicts from events.jsonl.

    Returns:
        Dict with keys:
            claimed_files (dict): filepath -> {"session_id": str, "since": str, "task": str}
            active_sessions (dict): session_id -> {"started": str, "files": list[str]}
            last_refresh (str): ISO8601 of last context_refreshed event, or "".
            context_hash (str): SHA-256 hash of AGENTS.md at last refresh, or "".
    """
    claimed_files: dict[str, dict] = {}
    active_sessions: dict[str, dict] = {}
    last_refresh = ""
    context_hash = ""

    for event in events:
        if event.get("status") != "committed":
            continue

        etype = event.get("event_type", "")
        payload = event.get("payload", {})
        session_id = event.get("session_id", "")
        ts = event.get("timestamp", "")

        if etype == "session_started":
            active_sessions[session_id] = {"started": ts, "files": []}

        elif etype == "session_ended":
            # Release all files owned by this session
            for fp, info in list(claimed_files.items()):
                if info.get("session_id") == session_id:
                    del claimed_files[fp]
            active_sessions.pop(session_id, None)

        elif etype == "file_claimed":
            fp = payload.get("filepath", "")
            claimed_files[fp] = {
                "session_id": session_id,
                "since": ts,
                "task": payload.get("task", ""),
            }
            if session_id in active_sessions:
                if fp not in active_sessions[session_id]["files"]:
                    active_sessions[session_id]["files"].append(fp)

        elif etype == "file_released":
            fp = payload.get("filepath", "")
            claimed_files.pop(fp, None)
            if session_id in active_sessions:
                files = active_sessions[session_id]["files"]
                if fp in files:
                    files.remove(fp)

        elif etype == "context_refreshed":
            last_refresh = ts
            context_hash = payload.get("context_hash", "")

    return {
        "claimed_files": claimed_files,
        "active_sessions": active_sessions,
        "last_refresh": last_refresh,
        "context_hash": context_hash,
    }


def _save_state(smm_dir: Path, state: dict) -> None:
    """Write materialised state to state.json (human-readable).

    Args:
        smm_dir: Path to .smm directory.
        state: Materialised state dict.
    """
    data = json.dumps(state, indent=2, default=str)
    target = _state_path(smm_dir)
    fd, tmp = tempfile.mkstemp(dir=str(target.parent), suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, str(target))
    except BaseException:
        os.unlink(tmp)
        raise


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------

def _validate(event_type: str, payload: dict, session_id: str, current_state: dict) -> tuple[bool, str]:
    """Validate a proposed event against current state.

    Args:
        event_type: One of the valid event type strings.
        payload: Event payload dict.
        session_id: ID of the proposing session.
        current_state: Materialised current state dict.

    Returns:
        Tuple of (accepted: bool, reason: str).
        reason is empty string on acceptance.
    """
    claimed = current_state.get("claimed_files", {})

    if event_type == "file_claimed":
        fp = payload.get("filepath", "")
        if fp in claimed and claimed[fp]["session_id"] != session_id:
            return False, f"{fp} is already claimed by session {claimed[fp]['session_id']}"
        return True, ""

    if event_type == "file_released":
        fp = payload.get("filepath", "")
        if fp not in claimed:
            return False, f"{fp} is not currently claimed"
        if claimed[fp]["session_id"] != session_id:
            return False, f"{fp} is owned by session {claimed[fp]['session_id']}, not {session_id}"
        return True, ""

    if event_type == "context_refreshed":
        new_hash = payload.get("context_hash", "")
        if new_hash and new_hash == current_state.get("context_hash", ""):
            return False, "AGENTS.md has not changed since last refresh"
        return True, ""

    if event_type in ("session_started", "session_ended"):
        return True, ""

    return False, f"Unknown event type: {event_type}"


# ---------------------------------------------------------------------------
# Public API: propose-validate-commit
# ---------------------------------------------------------------------------

def propose(smm_dir: Path, event_type: str, session_id: str, payload: dict) -> dict:
    """Propose a state change. Validates and commits if accepted.

    This is the single entry point for all state changes. Uses file
    locking to prevent concurrent writes from corrupting the event log.

    Args:
        smm_dir: Path to .smm directory.
        event_type: One of: file_claimed, file_released, context_refreshed,
                    session_started, session_ended.
        session_id: Identifier of the proposing session.
        payload: Event-type-specific payload dict.

    Returns:
        Dict with keys:
            accepted (bool): True if event was committed.
            event_id (str): ID assigned to this event.
            reason (str): Rejection reason if accepted=False, else "".
    """
    if _HAS_FILELOCK:
        with _FileLock(str(_lock_path(smm_dir))):
            return _propose_locked(smm_dir, event_type, session_id, payload)
    else:
        return _propose_locked(smm_dir, event_type, session_id, payload)


def _propose_locked(smm_dir: Path, event_type: str, session_id: str, payload: dict) -> dict:
    events = _read_events(smm_dir)
    current_state = materialize_state(events)

    accepted, reason = _validate(event_type, payload, session_id, current_state)

    event = {
        "event_id": _generate_event_id(),
        "event_type": event_type,
        "session_id": session_id,
        "payload": payload,
        "timestamp": _now_iso(),
        "status": "committed" if accepted else "rejected",
    }
    if not accepted:
        event["rejection_reason"] = reason

    _append_event(smm_dir, event)

    if accepted:
        new_state = materialize_state(_read_events(smm_dir))
        _save_state(smm_dir, new_state)

    return {
        "accepted": accepted,
        "event_id": event["event_id"],
        "reason": reason,
    }


# ---------------------------------------------------------------------------
# Convenience wrappers (used by CLI and coordinator integration)
# ---------------------------------------------------------------------------

def get_current_state(smm_dir: Path) -> dict:
    """Return the current materialised state.

    Args:
        smm_dir: Path to .smm directory.

    Returns:
        Materialised state dict (see materialize_state for shape).
    """
    path = _state_path(smm_dir)
    if path.exists():
        text = path.read_text(encoding="utf-8").strip()
        if text:
            return json.loads(text)
    return materialize_state(_read_events(smm_dir))


def read_events(smm_dir: Path) -> list[dict]:
    """Return all events from events.jsonl (committed and rejected).

    Args:
        smm_dir: Path to .smm directory.

    Returns:
        List of event dicts in insertion order.
    """
    return _read_events(smm_dir)
