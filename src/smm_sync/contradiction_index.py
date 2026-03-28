"""Contradiction pair index — tracks actioned contradictions to prevent re-flagging.

Stores a JSON file at .smm/contradiction_index.json.  Every contradiction pair
that has been actioned (resolved, deferred, or ignored) is recorded here.
Before any contradiction is surfaced to the developer, this index is checked;
already-actioned pairs are silently skipped.

Pair matching is order-independent: ("A", "B") == ("B", "A").
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

_INDEX_FILENAME = "contradiction_index.json"


# ---------------------------------------------------------------------------
# Read / write helpers
# ---------------------------------------------------------------------------

def load_index(smm_dir: Path) -> dict:
    """Load the contradiction index from .smm/contradiction_index.json.

    Args:
        smm_dir: Path to the .smm/ directory.

    Returns:
        Index dict with a 'pairs' key. Returns empty index on any error.
    """
    path = smm_dir / _INDEX_FILENAME
    if not path.exists():
        return {"pairs": []}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            return {"pairs": []}
        data.setdefault("pairs", [])
        return data
    except Exception:
        return {"pairs": []}


def save_index(smm_dir: Path, index: dict) -> None:
    """Atomically write the contradiction index.

    Args:
        smm_dir: Path to the .smm/ directory.
        index: Index dict to write.
    """
    smm_dir.mkdir(parents=True, exist_ok=True)
    path = smm_dir / _INDEX_FILENAME
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(index, indent=2), encoding="utf-8")
    tmp.replace(path)


# ---------------------------------------------------------------------------
# Pair-key helpers (order-independent)
# ---------------------------------------------------------------------------

def _pair_key(title_a: str, title_b: str) -> frozenset:
    """Return a canonical, order-independent key for two titles."""
    return frozenset({title_a.lower().strip(), title_b.lower().strip()})


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def is_actioned(index: dict, title_a: str, title_b: str) -> bool:
    """Return True if this pair has already been resolved, deferred, or ignored.

    Args:
        index: Index dict from load_index().
        title_a: First decision title.
        title_b: Second decision title.

    Returns:
        True when the pair exists in the index with an actioned status.
    """
    key = _pair_key(title_a, title_b)
    for pair in index.get("pairs", []):
        existing_key = _pair_key(
            pair.get("decision_a_title", ""),
            pair.get("decision_b_title", ""),
        )
        if key == existing_key:
            return pair.get("status", "") in ("resolved", "deferred", "ignored")
    return False


def record_action(
    smm_dir: Path,
    title_a: str,
    title_b: str,
    status: str,
    note: str = "",
    actor: str = "dev",
) -> None:
    """Record (or update) an action on a contradiction pair.

    Creates a new entry when the pair is not in the index; updates the
    existing entry when it is (e.g. promoting deferred → resolved).

    Args:
        smm_dir: Path to .smm/ directory.
        title_a: First decision title (usually the new one).
        title_b: Second decision title (usually the conflicting existing one).
        status: One of 'resolved', 'deferred', 'ignored'.
        note: Optional resolution note.
        actor: Who performed the action ('dev', 'dashboard', 'system').
    """
    index = load_index(smm_dir)
    key = _pair_key(title_a, title_b)
    now = datetime.now(timezone.utc).isoformat()

    for pair in index.get("pairs", []):
        existing_key = _pair_key(
            pair.get("decision_a_title", ""),
            pair.get("decision_b_title", ""),
        )
        if key == existing_key:
            pair["status"] = status
            pair["actioned_at"] = now
            pair["actioned_by"] = actor
            if note:
                pair["note"] = note
            save_index(smm_dir, index)
            return

    entry: dict = {
        "decision_a_title": title_a,
        "decision_b_title": title_b,
        "status": status,
        "actioned_at": now,
        "actioned_by": actor,
    }
    if note:
        entry["note"] = note
    index.setdefault("pairs", []).append(entry)
    save_index(smm_dir, index)


def filter_new_contradictions(
    smm_dir: Path,
    contradictions: list[dict],
    new_title: str = "",
) -> list[dict]:
    """Remove already-actioned pairs from a raw contradiction list.

    Supports two dict formats:
    - ``{"existing": str, ...}``   — output of GraphClient.contradiction_check()
      (title_a = new_title arg; title_b = dict["existing"])
    - ``{"decision_a": str, "decision_b": str, ...}``  — from _detect_local_contradictions

    Args:
        smm_dir: Path to .smm/ directory.
        contradictions: Raw contradiction list from the detector.
        new_title: Title of the new decision (used when format is "existing").

    Returns:
        Filtered list containing only new, unactioned contradictions.
    """
    if not contradictions:
        return []
    index = load_index(smm_dir)
    out: list[dict] = []
    for c in contradictions:
        if "existing" in c:
            a, b = new_title, c["existing"]
        else:
            a, b = c.get("decision_a", ""), c.get("decision_b", "")
        if a and b and not is_actioned(index, a, b):
            out.append(c)
    return out
