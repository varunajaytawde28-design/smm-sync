"""Pure-Python JSONL writer for smm add-decision — hot-path fallback.

No Kuzu, no sentence-transformers, no LLM calls.  Target: < 500 ms.

Used when the Rust binary (smm-fast-write) is not available or
the SMM_NO_RUST=1 environment variable is set.

Both this writer and the Rust binary produce identical JSONL format so
downstream tools (smm check, dashboard, smm get-context) work identically
regardless of which writer was used.

Output line format:
    {"uuid":"...","title":"...","rationale":"...","type":"...","confidence":0.9,
     "alternatives":"...","constraints":"...","timestamp":"2026-03-27T04:00:00Z",
     "project":"...","source":"manual","made_by":"lore-hook"}
"""
from __future__ import annotations

import hashlib
import json
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path


def _find_smm_dir() -> Path | None:
    """Walk up from cwd looking for a .smm/ directory.

    Mirrors the logic in the Rust binary so both writers locate the same
    directory.

    Returns:
        Path to the .smm/ directory, or None if not found.
    """
    current = Path.cwd()
    while True:
        candidate = current / ".smm"
        if candidate.is_dir():
            return candidate
        parent = current.parent
        if parent == current:
            return None
        current = parent


_TYPE_MAP: dict[str, str] = {
    "architectural": "architectural", "infrastructure": "architectural",
    "architecture": "architectural", "deployment": "architectural",
    "technical": "technical", "data-storage": "technical",
    "data_storage": "technical", "database": "technical",
    "framework": "technical", "security": "technical",
    "async-processing": "technical", "async_processing": "technical",
    "api-design": "technical", "api_design": "technical",
    "query-strategy": "technical", "query_strategy": "technical",
    "testing": "technical",
    "product": "product", "feature": "product", "business": "product",
    "constraint": "constraint", "limitation": "constraint",
}


def _normalize_type(t: str) -> str:
    """Normalize a raw type string to one of the 4 canonical values.

    Args:
        t: Raw type from CLI or dashboard.

    Returns:
        One of: architectural, technical, product, constraint.
    """
    return _TYPE_MAP.get((t or "").strip().lower(), "technical")


def _get_last_hash_lineage(lineage_path: Path) -> str:
    """Return the content_hash of the last entry in the lineage file.

    Args:
        lineage_path: Path to compliance_lineage.jsonl.

    Returns:
        Hex hash string, or "GENESIS" if file is empty or absent.
    """
    if not lineage_path.exists():
        return "GENESIS"
    try:
        last: dict | None = None
        with open(lineage_path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    try:
                        last = json.loads(line)
                    except Exception:
                        pass
        if last:
            return last.get("content_hash", "GENESIS")
    except Exception:
        pass
    return "GENESIS"


def _write_audit_hashed(lineage_path: Path, audit: dict) -> None:
    """Append an audit entry to compliance_lineage.jsonl with SHA-256 hash chain.

    Computes a canonical content_hash of the entry (without hash fields),
    deduplicates by that hash to prevent duplicate entries from re-syncs,
    then writes with content_hash and prev_hash fields.

    Args:
        lineage_path: Path to compliance_lineage.jsonl.
        audit: Entry dict to write (must not contain content_hash/prev_hash).
    """
    base = {k: v for k, v in audit.items() if k not in ("content_hash", "prev_hash")}
    canonical = json.dumps(base, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    content_hash = hashlib.sha256(canonical.encode()).hexdigest()

    # Dedup: skip if an entry with this exact content_hash already exists
    if lineage_path.exists():
        try:
            with open(lineage_path, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        if json.loads(line).get("content_hash") == content_hash:
                            return
                    except Exception:
                        pass
        except Exception:
            pass

    audit["prev_hash"] = _get_last_hash_lineage(lineage_path)
    audit["content_hash"] = content_hash
    lineage_path.parent.mkdir(parents=True, exist_ok=True)
    with open(lineage_path, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(audit, ensure_ascii=False) + "\n")


def _normalize_context(ctx: dict, made_by: str = "") -> dict:
    """Normalize decision context, filling defaults.

    Args:
        ctx: Raw context dict (may have partial keys).
        made_by: The made_by field to infer source when not provided.

    Returns:
        Dict with source, trigger, git_ref, branch keys.
    """
    if not ctx:
        ctx = {}
    default_source = "git-commit" if ("lore-hook" in made_by or "git" in made_by) else "manual"
    return {
        "source": str(ctx.get("source") or default_source),
        "trigger": str(ctx.get("trigger") or "")[:200],
        "git_ref": str(ctx.get("git_ref") or ""),
        "branch": str(ctx.get("branch") or ""),
    }


def _value_to_str(v: object) -> str:
    """Convert a list or string value to a semicolon-separated string.

    Args:
        v: A list of strings, a bare string, or None.

    Returns:
        Semicolon-joined string, or empty string.
    """
    if isinstance(v, list):
        return "; ".join(str(x) for x in v if x)
    if isinstance(v, str):
        return v
    return ""


def _validated_confidence(raw: object) -> float:
    """Parse and validate a confidence value.

    Args:
        raw: Raw confidence value from input (float, int, str, or None).

    Returns:
        Float in [0.0, 1.0].

    Raises:
        ValueError: If the value is outside [0.0, 1.0].
    """
    if raw is None:
        return 0.80
    val = float(raw)
    if not (0.0 <= val <= 1.0):
        raise ValueError(
            f"Confidence must be between 0.0 and 1.0, got {raw!r}"
        )
    return val


def append_jsonl_locked(path: Path, record: dict, retries: int = 3, retry_delay: float = 1.0) -> bool:
    """Append one JSON line to a JSONL file using an exclusive file lock.

    Safe for concurrent writers (e.g. background smm check + dashboard).
    Uses fcntl.flock on POSIX; falls back to unlocked append on Windows or
    when fcntl is unavailable.

    Args:
        path: Target .jsonl file path.
        record: Dict to serialise and append.
        retries: Number of lock-acquire attempts before giving up.
        retry_delay: Seconds to wait between retry attempts.

    Returns:
        True on success, False if the lock could not be acquired after all
        retries (the write is skipped and a warning is printed to stderr).
    """
    import sys as _sys

    line = json.dumps(record, ensure_ascii=False) + "\n"
    path.parent.mkdir(parents=True, exist_ok=True)

    try:
        import fcntl  # POSIX only
    except ImportError:
        # Windows or unusual environment — just append without locking
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(line)
        return True

    for attempt in range(retries):
        try:
            with open(path, "a", encoding="utf-8") as fh:
                fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
                try:
                    fh.write(line)
                finally:
                    fcntl.flock(fh, fcntl.LOCK_UN)
            return True
        except BlockingIOError:
            if attempt < retries - 1:
                time.sleep(retry_delay)
        except Exception as exc:
            print(f"[smm] append_jsonl_locked error: {exc}", file=_sys.stderr)
            return False

    print(
        f"[smm] append_jsonl_locked: could not acquire lock on {path} after "
        f"{retries} attempts — skipping write",
        file=_sys.stderr,
    )
    return False


def write_decision(data: dict, project: str = "smm-sync") -> str:
    """Append one decision line to .smm/decisions.jsonl.

    Also appends an audit entry to .smm/compliance_lineage.jsonl
    (best-effort; never raises on audit failure).

    Args:
        data: Decision dict.  Required keys: title, rationale, type.
              Optional: confidence, alternatives, constraints, project,
              source, made_by.
        project: Default project name used when data["project"] is absent.

    Returns:
        UUID string of the written decision.

    Raises:
        ValueError: If a required field (title, rationale, type) is missing.
        RuntimeError: If .smm/ cannot be found and the config module
                      cannot supply an alternative path.
    """
    title = (data.get("title") or "").strip()
    if not title:
        raise ValueError("Missing required field: title")

    rationale = (data.get("rationale") or "").strip()
    if not rationale:
        raise ValueError("Missing required field: rationale")

    decision_type = _normalize_type(
        (data.get("type") or data.get("decision_type") or "technical").strip()
    )

    # Locate .smm/
    smm_dir = _find_smm_dir()
    if smm_dir is None:
        try:
            from smm_sync.config import get_smm_dir
            smm_dir = get_smm_dir()
        except Exception:
            raise RuntimeError(
                "Could not find .smm/ directory. Run: smm init"
            )

    decision_uuid = str(uuid.uuid4())
    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    record: dict = {
        "uuid": decision_uuid,
        "title": title,
        "rationale": rationale,
        "type": decision_type,
        "confidence": _validated_confidence(data.get("confidence")),
        "alternatives": _value_to_str(data.get("alternatives", "")),
        "constraints": _value_to_str(data.get("constraints", "")),
        "timestamp": timestamp,
        "project": data.get("project") or project,
        "source": data.get("source") or "manual",
        "made_by": data.get("made_by") or "lore-hook",
    }
    # Normalize and attach context (EU AI Act Art 12 reference source)
    _raw_ctx = data.get("context")
    if isinstance(_raw_ctx, str) and _raw_ctx:
        _raw_ctx = {"source": _raw_ctx}
    record["context"] = _normalize_context(_raw_ctx or {}, record["made_by"])

    # Atomic POSIX append: O_APPEND writes < PIPE_BUF are atomic on POSIX.
    decisions_path = smm_dir / "decisions.jsonl"
    line = json.dumps(record, ensure_ascii=False) + "\n"
    with open(decisions_path, "a", encoding="utf-8") as fh:
        fh.write(line)

    # Audit entry — best-effort, never blocks the caller.
    try:
        audit = {
            "entry_id": str(uuid.uuid4()),
            "timestamp": timestamp,
            "event_type": "decision_recorded",
            "decision_uuid": decision_uuid,
            "title": title,
            "source": record["source"],
            "made_by": record["made_by"],
            "context_source": record["context"]["source"],
            "context_git_ref": record["context"]["git_ref"],
        }
        lineage_path = smm_dir / "compliance_lineage.jsonl"
        _write_audit_hashed(lineage_path, audit)
    except Exception:
        pass

    return decision_uuid
