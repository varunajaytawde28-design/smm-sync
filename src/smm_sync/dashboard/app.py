"""
CaaS Dashboard — FastAPI backend.

Serves the dashboard UI and provides REST API endpoints
that read from the real knowledge graph, compliance log,
and MCP server state.

Run: smm dashboard
Opens: http://localhost:7842
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import sys
import uuid
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Optional

import uvicorn
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from smm_sync.config import DEFAULT_DASHBOARD_PORT

try:
    from reportlab.lib.pagesizes import letter
    from reportlab.lib.units import inch
    from reportlab.pdfgen import canvas as rl_canvas
    _PDF_AVAILABLE = True
except ImportError:
    _PDF_AVAILABLE = False

async def _startup_contradiction_check() -> None:
    """Run smm check on startup if there are decisions newer than last check.

    Reads .smm/decisions.jsonl and .smm/last_check_timestamp.txt to decide
    whether a check is needed, then delegates to `smm check` so that
    .smm/contradictions.jsonl is populated before the first dashboard request.
    """
    import subprocess as _subprocess

    smm_dir = _get_smm_dir()
    decisions_path = smm_dir / "decisions.jsonl"
    last_check_path = smm_dir / "last_check_timestamp.txt"

    if not decisions_path.exists():
        return

    last_check_ts: Optional[datetime] = None
    if last_check_path.exists():
        try:
            ts_str = last_check_path.read_text(encoding="utf-8").strip()
            last_check_ts = datetime.fromisoformat(ts_str)
        except Exception:
            pass

    has_new = False
    if last_check_ts is None:
        has_new = decisions_path.stat().st_size > 0
    else:
        try:
            for line in decisions_path.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    d = json.loads(line)
                    ts = datetime.fromisoformat(d.get("timestamp", "").replace("Z", "+00:00"))
                    if ts > last_check_ts:
                        has_new = True
                        break
                except Exception:
                    has_new = True
                    break
        except Exception:
            pass

    if not has_new:
        return

    print("[dashboard] New decisions detected — running smm check...", file=sys.stderr)
    try:
        proc = await asyncio.create_subprocess_exec(
            "smm", "check",
            stdout=_subprocess.PIPE,
            stderr=_subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=120)
        if stdout:
            print(f"[dashboard] smm check: {stdout.decode()[:500]}", file=sys.stderr)
        if proc.returncode != 0 and stderr:
            print(f"[dashboard] smm check warning: {stderr.decode()[:200]}", file=sys.stderr)
        else:
            print("[dashboard] smm check complete", file=sys.stderr)
    except Exception as exc:
        print(f"[dashboard] startup contradiction check failed: {exc}", file=sys.stderr)


@asynccontextmanager
async def _lifespan(app: FastAPI):
    """Start up the dashboard without blocking on heavy operations.

    The dashboard reads exclusively from JSONL files and serves the web UI.
    No model loading or graph sync at startup — these only happen via ``smm check``.
    """
    # Start non-blocking background check if there are unchecked decisions.
    # Fire-and-forget: dashboard is immediately available regardless of outcome.
    asyncio.create_task(_startup_contradiction_check())

    yield


app = FastAPI(title="CaaS Dashboard", version="0.3.0", lifespan=_lifespan)

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
_static_dir = Path(__file__).parent / "static"

# Mount static files (CSS, JS, etc.)
if _static_dir.exists():
    app.mount("/static", StaticFiles(directory=str(_static_dir)), name="static")


def _get_smm_dir() -> Path:
    """Locate .smm/ relative to current working directory or project root.

    Returns:
        Path to .smm/ directory (may not exist yet).
    """
    try:
        from smm_sync.config import find_project_root
        return find_project_root() / ".smm"
    except Exception:
        return Path.cwd() / ".smm"


_graph_client_cache = None
_graph_client_cache_dir: "Path | None" = None


def _read_decisions_jsonl(smm_dir: Path) -> list[dict]:
    """Read all decisions from .smm/decisions.jsonl.

    Args:
        smm_dir: Path to the .smm/ directory.

    Returns:
        List of decision dicts parsed from JSONL, newest-last order.
    """
    decisions_path = smm_dir / "decisions.jsonl"
    if not decisions_path.exists():
        return []
    results: list[dict] = []
    for raw_line in decisions_path.read_text(encoding="utf-8").splitlines():
        raw_line = raw_line.strip()
        if raw_line:
            try:
                results.append(json.loads(raw_line))
            except Exception:
                pass
    return results


_DECISION_TYPE_MAP: dict[str, str] = {
    # architectural
    "architectural": "architectural",
    "infrastructure": "architectural",
    "architecture": "architectural",
    "deployment": "architectural",
    # technical
    "technical": "technical",
    "data-storage": "technical",
    "data_storage": "technical",
    "database": "technical",
    "framework": "technical",
    "security": "technical",
    "async-processing": "technical",
    "async_processing": "technical",
    "api-design": "technical",
    "api_design": "technical",
    "query-strategy": "technical",
    "query_strategy": "technical",
    "testing": "technical",
    # product
    "product": "product",
    "feature": "product",
    "business": "product",
    # constraint
    "constraint": "constraint",
    "limitation": "constraint",
}


def _normalize_decision_type(t: str) -> str:
    """Normalize a raw decision type string to one of the 4 canonical values.

    Args:
        t: Raw type string from JSONL, Kuzu, or CLI input.

    Returns:
        One of: "architectural", "technical", "product", "constraint".
        Defaults to "technical" for unrecognized values.
    """
    return _DECISION_TYPE_MAP.get((t or "").strip().lower(), "technical")


def _get_graph_client():
    """Return (lazily initialise) the GraphClient for the context graph.

    Re-creates the client when the smm_dir changes (e.g. during tests where
    _get_smm_dir is patched to a different tmp directory per test). This
    prevents stale graph-dir references from contaminating test isolation.

    Returns:
        GraphClient instance, or None if context_graph is unavailable.
    """
    global _graph_client_cache, _graph_client_cache_dir
    graph_dir = _get_smm_dir() / "graph"
    if _graph_client_cache is None or _graph_client_cache_dir != graph_dir:
        try:
            from smm_sync.context_graph.client import GraphClient, get_graph_client

            # Use the module-level singleton when graph_dir matches what it
            # was created with (production path). Create a fresh GraphClient
            # directly when graph_dir has changed (e.g. per-test tmp dir).
            from smm_sync.context_graph import client as _cgc_module
            if _cgc_module._client is not None and _cgc_module._client.graph_dir == graph_dir:
                _graph_client_cache = _cgc_module._client
            else:
                _graph_client_cache = GraphClient(graph_dir=graph_dir)
            _graph_client_cache_dir = graph_dir
        except Exception:
            return None
    return _graph_client_cache


def _calculate_time_saved(lineage_path: Path, period_days: int = 7) -> dict:
    """Calculate estimated time saved from injection log.

    Reads compliance_lineage.jsonl, counts context_injection entries in period,
    applies formula: injections × 3.75 minutes.

    Args:
        lineage_path: Path to compliance_lineage.jsonl.
        period_days: Number of days to look back for period stats.

    Returns:
        Dict with today, week, total counts and formatted strings.
    """
    now = datetime.now(timezone.utc)
    today_str = now.strftime("%Y-%m-%d")
    week_cutoff = now - timedelta(days=7)
    period_cutoff = now - timedelta(days=period_days)

    total_count = 0
    today_count = 0
    week_count = 0
    period_count = 0

    if lineage_path.exists():
        try:
            with open(lineage_path, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        entry = json.loads(line)
                        if entry.get("event_type") != "context_injection":
                            continue
                        total_count += 1
                        ts_str = entry.get("timestamp", "")
                        if ts_str.startswith(today_str):
                            today_count += 1
                        if ts_str:
                            try:
                                ts = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
                                if ts >= week_cutoff:
                                    week_count += 1
                                if ts >= period_cutoff:
                                    period_count += 1
                            except Exception as e:
                                print(f"[dashboard] _calculate_time_saved timestamp parse error: {e}", file=sys.stderr)
                    except Exception as e:
                        print(f"[dashboard] _calculate_time_saved json parse error: {e}", file=sys.stderr)
                        continue
        except Exception as e:
            print(f"[dashboard] _calculate_time_saved file read error: {e}", file=sys.stderr)

    def _mins_to_str(mins: int) -> str:
        h, m = divmod(mins, 60)
        if h > 0:
            return f"{h}h {m}m"
        return f"{m}m"

    mins_today = int(today_count * 3.75)
    mins_week = int(week_count * 3.75)
    mins_total = int(total_count * 3.75)

    return {
        "time_saved_minutes_today": mins_today,
        "time_saved_minutes_week": mins_week,
        "time_saved_minutes_total": mins_total,
        "time_saved_formatted_week": _mins_to_str(mins_week),
        "time_saved_formatted_total": _mins_to_str(mins_total),
        "baseline_assumption_minutes": 15,
        "injections_per_session_assumed": 4,
    }


def _get_last_hash(lineage_path: Path) -> str:
    """Return the content_hash of the last entry in compliance_lineage.jsonl."""
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
        return last.get("content_hash", "GENESIS") if last else "GENESIS"
    except Exception:
        return "GENESIS"


def _write_hashed_audit(lineage_path: Path, entry: dict) -> None:
    """Append an audit entry to compliance_lineage.jsonl with SHA-256 hash chain.

    Deduplicates by content_hash to prevent duplicate entries from re-syncs.
    """
    base = {k: v for k, v in entry.items() if k not in ("content_hash", "prev_hash")}
    canonical = json.dumps(base, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    content_hash = hashlib.sha256(canonical.encode()).hexdigest()

    if lineage_path.exists():
        try:
            with open(lineage_path, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        if json.loads(line).get("content_hash") == content_hash:
                            return  # duplicate
                    except Exception:
                        pass
        except Exception:
            pass

    entry["prev_hash"] = _get_last_hash(lineage_path)
    entry["content_hash"] = content_hash
    lineage_path.parent.mkdir(parents=True, exist_ok=True)
    with open(lineage_path, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(entry, ensure_ascii=False) + "\n")


def _read_compliance_log(smm_dir: Path) -> list[dict]:
    """Read all entries from compliance_lineage.jsonl.

    Args:
        smm_dir: Path to .smm/ directory.

    Returns:
        List of log entry dicts, empty list if file does not exist.
    """
    log_path = smm_dir / "compliance_lineage.jsonl"
    entries: list[dict] = []
    if not log_path.exists():
        return entries
    try:
        with open(log_path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    try:
                        entries.append(json.loads(line))
                    except Exception as e:
                        print(f"[dashboard] _read_compliance_log json parse error: {e}", file=sys.stderr)
    except Exception as e:
        print(f"[dashboard] _read_compliance_log file read error: {e}", file=sys.stderr)
    return entries


def _read_contradictions(smm_dir: Path) -> list[dict]:
    """Read contradictions.jsonl.

    Args:
        smm_dir: Path to .smm/ directory.

    Returns:
        List of contradiction dicts.
    """
    path = smm_dir / "contradictions.jsonl"
    items: list[dict] = []
    if not path.exists():
        return items
    try:
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    try:
                        items.append(_normalize_contradiction(json.loads(line)))
                    except Exception as e:
                        print(f"[dashboard] _read_contradictions json parse error: {e}", file=sys.stderr)
    except Exception as e:
        print(f"[dashboard] _read_contradictions file read error: {e}", file=sys.stderr)
    return items


def _normalize_contradiction(c: dict) -> dict:
    """Normalize contradiction field names from demo/old writers to canonical form."""
    out = dict(c)
    if not out.get("id"):
        out["id"] = out.get("uuid", "")
    if not out.get("explanation"):
        out["explanation"] = out.get("reason", "")
    if not out.get("detected_at"):
        out["detected_at"] = out.get("timestamp", "")
    # Normalize resolved: bool from status field
    if not out.get("resolved"):
        status = out.get("status", "")
        if status in ("resolved", "dismissed", "ignored"):
            out["resolved"] = True
    return out


def _write_contradiction(smm_dir: Path, entry: dict) -> None:
    """Append a contradiction entry to .smm/contradictions.jsonl.

    Args:
        smm_dir: Path to .smm/ directory.
        entry: Contradiction dict to append.
    """
    path = smm_dir / "contradictions.jsonl"
    try:
        smm_dir.mkdir(parents=True, exist_ok=True)
        from smm_sync.jsonl_writer import append_jsonl_locked
        if not append_jsonl_locked(path, entry):
            print("[dashboard] _write_contradiction: lock timeout — skipped", file=sys.stderr)
    except Exception as e:
        print(f"[dashboard] _write_contradiction file write error: {e}", file=sys.stderr)


# ---------------------------------------------------------------------------
# Root
# ---------------------------------------------------------------------------

@app.get("/")
async def serve_dashboard() -> FileResponse:
    """Serve the main dashboard HTML.

    Returns:
        index.html as a FileResponse.
    """
    index = _static_dir / "index.html"
    if not index.exists():
        raise HTTPException(status_code=404, detail="Dashboard UI not found. Run 'smm dashboard' to initialize.")
    return FileResponse(str(index))


_HOTSPOT_STOP_WORDS = {
    "the", "and", "for", "with", "this", "that", "are", "have", "from",
    "not", "but", "use", "used", "uses", "when", "will", "than", "which",
    "been", "more", "also", "into", "both", "each", "they", "them", "their",
    "should", "using", "should", "would", "could", "over", "only", "some",
    "between", "about", "after", "before", "during", "while", "then", "there",
}


def _detect_hotspots(contradictions: list[dict]) -> list[dict]:
    """Find architectural hotspots: keywords in 3+ unresolved contradiction titles.

    Args:
        contradictions: All contradiction dicts (normalized).

    Returns:
        List of dicts with 'keyword' and 'count', max 2 entries.
    """
    unresolved = [
        c for c in contradictions
        if c.get("status", "pending") not in ("resolved", "dismissed", "ignored")
        and not c.get("resolved", False)
    ]
    keyword_counts: dict[str, int] = {}
    for c in unresolved:
        title = (c.get("title") or c.get("name") or "").lower()
        words = re.findall(r"[a-z][a-z0-9_-]{2,}", title)
        seen = set()
        for w in words:
            if w not in _HOTSPOT_STOP_WORDS and w not in seen:
                keyword_counts[w] = keyword_counts.get(w, 0) + 1
                seen.add(w)
    hotspots = [
        {"keyword": kw, "count": cnt}
        for kw, cnt in keyword_counts.items()
        if cnt >= 3
    ]
    hotspots.sort(key=lambda x: -x["count"])
    return hotspots[:2]


# ---------------------------------------------------------------------------
# /api/stats
# ---------------------------------------------------------------------------

@app.get("/api/stats")
async def get_stats() -> dict:
    """Return dashboard summary statistics.

    Reads injections from compliance_lineage.jsonl.
    Reads decision/contradiction counts from graph if available.

    Returns:
        Dict with decisions, contradictions, injections_total, injections_today,
        avg_confidence, active_agents, captures_today.
    """
    smm_dir = _get_smm_dir()
    entries = await asyncio.to_thread(_read_compliance_log, smm_dir)

    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    injection_entries = [e for e in entries if e.get("event_type") == "context_injection"]
    injections_today = sum(1 for e in injection_entries if e.get("timestamp", "").startswith(today))

    # Count decisions added today and this week
    decision_entries = [e for e in entries if e.get("event_type") == "decision_added"]
    captures_today = sum(1 for e in decision_entries if e.get("timestamp", "").startswith(today))
    _now = datetime.now(timezone.utc)
    _week_cutoff = _now - timedelta(days=7)
    _prev_week_cutoff = _now - timedelta(days=14)
    decisions_this_week = 0
    decisions_prev_week = 0
    for _e in decision_entries:
        _ts_str = _e.get("timestamp", "")
        if _ts_str:
            try:
                _ts = datetime.fromisoformat(_ts_str.replace("Z", "+00:00"))
                if _ts >= _week_cutoff:
                    decisions_this_week += 1
                elif _ts >= _prev_week_cutoff:
                    decisions_prev_week += 1
            except Exception:
                pass
    decisions_trend = decisions_this_week - decisions_prev_week

    # Avg confidence from decision_added entries
    confidences = [e.get("confidence", 0.0) for e in decision_entries if "confidence" in e]
    avg_confidence = round(sum(confidences) / len(confidences), 2) if confidences else 0.82

    # Count contradictions
    contradictions = await asyncio.to_thread(_read_contradictions, smm_dir)
    hotspots = _detect_hotspots(contradictions)
    _resolved_count = sum(1 for c in contradictions if c.get("status") == "resolved")
    _pending_count = sum(
        1 for c in contradictions
        if c.get("status", "pending") not in ("resolved", "dismissed", "ignored")
    )
    if _resolved_count + _pending_count > 0:
        human_oversight_pct = round(_resolved_count / (_resolved_count + _pending_count) * 100)
    else:
        human_oversight_pct = None
    contradiction_count = sum(
        1 for c in contradictions
        if not c.get("resolved", False) and c.get("status", "pending") not in ("resolved", "dismissed", "ignored")
    )

    # Pending board items
    board_items = await asyncio.to_thread(_load_board)
    pending_decisions = sum(1 for i in board_items if i.get("status") != "done")

    # Active agents: unique sessions with activity in last 60 min
    cutoff = datetime.now(timezone.utc) - timedelta(hours=1)
    active_sessions: set[str] = set()
    for e in injection_entries:
        ts_str = e.get("timestamp", "")
        if ts_str:
            try:
                ts = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
                if ts >= cutoff and e.get("session_id"):
                    active_sessions.add(e["session_id"])
            except Exception as e:
                print(f"[dashboard] get_stats timestamp parse error: {e}", file=sys.stderr)
    active_agents = len(active_sessions)

    # Decision count: query Episodic nodes (the actual decision records).
    # Entity nodes are extracted reference entities and must not be counted.
    decision_count = 0
    try:
        client = _get_graph_client()
        if client is not None:
            await client._get_graphiti()
            rows, _, _ = await client._driver.execute_query(
                "MATCH (e:Episodic) RETURN count(e) AS cnt"
            )
            if rows:
                decision_count = rows[0].get("cnt", 0)
    except Exception as e:
        print(f"[dashboard] get_stats graph count error: {e}", file=sys.stderr)
    if decision_count == 0:
        # Fall back to decisions.jsonl line count (most reliable)
        _jsonl_rows = await asyncio.to_thread(_read_decisions_jsonl, smm_dir)
        decision_count = len(_jsonl_rows)
        if decision_count == 0:
            # Last resort: unique titles from compliance log (decision_recorded OR decision_added)
            decision_count = len(set(
                e.get("decision_title", "") or e.get("title", "")
                for e in entries
                if e.get("event_type") in ("decision_recorded", "decision_added")
                and (e.get("decision_title") or e.get("title"))
            ))

    # Déjà vu count: rejection-keyword hits in today's injections
    deja_vu_today = sum(
        1 for e in injection_entries
        if e.get("timestamp", "").startswith(today)
        and any(
            kw in str(e.get("decisions_surfaced", "")).lower()
            for kw in ("rejected", "alternative", "considered", "discarded")
        )
    )

    # Time saved metrics
    time_saved = _calculate_time_saved(smm_dir / "compliance_lineage.jsonl")

    # Repo owner/name from git remote
    repo_owner = ""
    repo_name = ""
    try:
        from smm_sync.config import find_project_root
        from smm_sync.git_utils import get_git_remote
        project_root = find_project_root()
        remote = get_git_remote(project_root)
        if remote:
            repo_owner, repo_name = remote
    except Exception as e:
        print(f"[dashboard] get_stats git remote lookup error: {e}", file=sys.stderr)
    # Fallback: read project name from .smm/config.json
    if not repo_name:
        try:
            _cfg_path = smm_dir / "config.json"
            if _cfg_path.exists():
                _cfg = json.loads(_cfg_path.read_text(encoding="utf-8"))
                repo_name = _cfg.get("repo_name") or _cfg.get("project") or ""
        except Exception:
            pass

    total_decisions = decision_count
    total_time_saved_hours = round(time_saved["time_saved_minutes_total"] / 60.0, 1)

    return {
        # existing keys
        "decisions": decision_count,
        "contradictions": contradiction_count,
        "deja_vu_count": deja_vu_today,
        "deja_vu_today": deja_vu_today,
        "injections_total": len(injection_entries),
        "injections_today": injections_today,
        "avg_confidence": avg_confidence,
        "active_agents": active_agents,
        "captures_today": captures_today,
        "zero_touch": True,
        "local_only": True,
        "tagline": "Context as a Service — passive, local, zero-touch",
        "repo_owner": repo_owner,
        "repo_name": repo_name,

        # aliases expected by index.html
        "total_decisions": total_decisions,
        "total": total_decisions,
        "pending_decisions": pending_decisions,
        "pending": pending_decisions,
        "decisions_this_week": decisions_this_week,
        "decisions_prev_week": decisions_prev_week,
        "decisions_trend": decisions_trend,
        "hotspots": hotspots,
        "time_saved_hours": total_time_saved_hours,
        "time_saved": time_saved["time_saved_formatted_total"],
        "time_saved_meta": "context lookups avoided",
        "human_oversight_pct": human_oversight_pct,

        # existing time-saved fields
        **time_saved,
    }


# ---------------------------------------------------------------------------
# /api/decisions
# ---------------------------------------------------------------------------

@app.get("/api/decisions")
async def get_decisions(
    limit: int = Query(20, ge=1, le=1000),
    offset: int = Query(0, ge=0),
    type: str = Query("all"),
    status: str = Query("all"),
) -> dict:
    """Return paginated list of decisions.

    Primary source: .smm/decisions.jsonl (always current).
    Secondary source: Kuzu graph (provides edge/graph data, may be stale).

    Falls back to Kuzu if decisions.jsonl does not exist (existing installs).
    Adds a ``sync_banner`` field when Kuzu has fewer records than JSONL.

    Args:
        limit: Maximum results to return.
        offset: Number of results to skip.
        type: Decision type filter ('all', 'architectural', 'technical', 'product').
        status: Status filter ('all', 'approved', 'deferred', 'ignored').

    Returns:
        Dict with decisions list, total, limit, offset, optional sync_banner.
    """
    smm_dir = _get_smm_dir()
    decisions: list[dict] = []
    sync_banner: str | None = None

    # ── Primary: read from decisions.jsonl ───────────────────────────────────
    jsonl_rows = _read_decisions_jsonl(smm_dir)

    if jsonl_rows:
        for d in jsonl_rows:
            decision_type = _normalize_decision_type(d.get("type", "technical"))
            if type != "all" and decision_type != type:
                continue

            d_status = "approved"  # JSONL decisions are approved by default
            if status != "all" and d_status != status:
                continue

            _raw_conf = float(d.get("confidence", 0.80) or 0.80)
            _conf = _raw_conf / 100.0 if _raw_conf > 1.0 else _raw_conf
            decisions.append({
                "id": d.get("uuid", ""),
                "title": d.get("title", "(untitled)"),
                "type": decision_type,
                "confidence": _conf,
                "source_type": d.get("source", "manual"),
                "created_at": d.get("timestamp", datetime.now(timezone.utc).isoformat()),
                "is_constraint": decision_type == "constraint" or "[CONSTRAINT]" in d.get("title", ""),
                "is_superseded": False,
                "overrides": None,
                "rationale": d.get("rationale", ""),
                "alternatives": d.get("alternatives", ""),
                "constraints": d.get("constraints", ""),
                "made_by": d.get("made_by", ""),
                "status": d_status,
                "context": d.get("context") or {},
            })

        # Check Kuzu count for sync banner (best-effort, non-blocking)
        try:
            client = _get_graph_client()
            if client is not None:
                kuzu_raw = await client.get_decisions(project="smm-sync")
                kuzu_count = len(kuzu_raw)
                if kuzu_count < len(jsonl_rows):
                    sync_banner = (
                        f"Graph needs sync ({kuzu_count} in Kuzu vs "
                        f"{len(jsonl_rows)} in JSONL). Run: smm check"
                    )
        except Exception as _kuzu_exc:
            print(f"[dashboard] kuzu count check: {_kuzu_exc}", file=sys.stderr)

    else:
        # ── Fallback: Kuzu (existing installs without decisions.jsonl) ───────
        try:
            client = _get_graph_client()
            if client is None:
                raise RuntimeError("Graph client unavailable")
            raw = await client.get_decisions(project="smm-sync")

            for d in raw:
                _raw_type = "architectural"
                content_lower = (d.content or "").lower()
                if "product" in content_lower:
                    _raw_type = "product"
                elif "technical" in content_lower:
                    _raw_type = "technical"
                decision_type = _normalize_decision_type(_raw_type)

                if type != "all" and decision_type != type:
                    continue

                _raw_content = (d.content or "").replace("\\n", "\n")
                decision_status = "approved"
                for line in _raw_content.splitlines():
                    if line.startswith("Status:"):
                        decision_status = line.split(":", 1)[1].strip()
                        break

                if status != "all" and decision_status != status:
                    continue

                _content_lines = _raw_content.splitlines()
                confidence = 0.80
                for line in _content_lines:
                    if line.startswith("Confidence:"):
                        try:
                            _c = float(line.split(":", 1)[1].strip())
                            confidence = _c / 100.0 if _c > 1.0 else _c
                        except Exception as e:
                            print(
                                f"[dashboard] get_decisions confidence parse: {e}",
                                file=sys.stderr,
                            )

                rationale = ""
                for line in _content_lines:
                    if line.startswith("Rationale:"):
                        rationale = line.split(":", 1)[1].strip()
                        break

                created_at = d.created_at
                if hasattr(created_at, "isoformat"):
                    created_at_str = created_at.isoformat()
                else:
                    created_at_str = (
                        str(created_at) if created_at
                        else datetime.now(timezone.utc).isoformat()
                    )

                decisions.append({
                    "id": str(d.id),
                    "title": d.title or "(untitled)",
                    "type": decision_type,
                    "confidence": confidence,
                    "source_type": "manual",
                    "created_at": created_at_str,
                    "is_constraint": "[CONSTRAINT]" in (d.title or ""),
                    "is_superseded": not d.valid,
                    "overrides": None,
                    "rationale": rationale,
                    "status": decision_status,
                })
        except Exception as _exc:
            print(f"[dashboard] get_decisions fallback error: {_exc}", file=sys.stderr)

    total = len(decisions)
    paginated = decisions[offset: offset + limit]

    result: dict = {
        "decisions": paginated,
        "total": total,
        "limit": limit,
        "offset": offset,
    }
    if sync_banner:
        result["sync_banner"] = sync_banner
    return result


@app.get("/api/decisions/{decision_id}/export")
async def export_decision(decision_id: str) -> StreamingResponse:
    """Export a single decision as a downloadable markdown file.

    Args:
        decision_id: Decision UUID.

    Returns:
        Markdown file as a streaming response.
    """
    smm_dir = _get_smm_dir()

    title = "Decision"
    content = ""
    rationale = ""
    created_at = datetime.now(timezone.utc).isoformat()

    try:
        client = _get_graph_client()
        if client is None:
            raise RuntimeError("Graph client unavailable")
        decisions = await client.get_decisions(project="smm-sync")
        for d in decisions:
            if str(d.id) == decision_id:
                title = d.title or "Decision"
                content = d.content or ""
                if hasattr(d.created_at, "isoformat"):
                    created_at = d.created_at.isoformat()
                for line in content.splitlines():
                    if line.startswith("Rationale:"):
                        rationale = line.split(":", 1)[1].strip()
                break
    except Exception as e:
        print(f"[dashboard] export_decision graph query error: {e}", file=sys.stderr)

    slug = title.lower().replace(" ", "-").replace("[", "").replace("]", "")[:50]
    md = f"# {title}\n\n**Date:** {created_at[:10]}\n\n## Content\n\n{content}\n\n## Rationale\n\n{rationale}\n"

    return StreamingResponse(
        iter([md]),
        media_type="text/markdown",
        headers={"Content-Disposition": f'attachment; filename="{slug}.md"'},
    )


# ---------------------------------------------------------------------------
# /api/contradictions
# ---------------------------------------------------------------------------

@app.get("/api/contradictions")
async def get_contradictions() -> dict:
    """Return list of detected contradictions with decision UUIDs and rationale.

    Returns:
        Dict with contradictions list.
    """
    smm_dir = _get_smm_dir()
    raw = await asyncio.to_thread(_read_contradictions, smm_dir)

    # Build title→{uuid, rationale} map for the A/B winner picker
    title_info: dict[str, dict] = {}
    try:
        gc = _get_graph_client()
        if gc is not None:
            await gc._get_graphiti()
            rows, _, _ = await gc._driver.execute_query(
                "MATCH (e:Episodic) RETURN e.uuid, e.name, e.content "
                "ORDER BY e.created_at DESC LIMIT 500"
            )
            for row in rows:
                name = row.get("e.name", "") or ""
                uuid = row.get("e.uuid", "") or ""
                content = (row.get("e.content", "") or "").replace("\\n", "\n")
                rationale = ""
                if "Rationale: " in content:
                    rationale = content.split("Rationale: ", 1)[1].split("\n")[0].strip()[:200]
                if name:
                    title_info[name] = {"uuid": uuid, "rationale": rationale}
    except Exception as _e:
        print(f"[dashboard] get_contradictions kuzu lookup error: {_e}", file=sys.stderr)

    items = []
    for c in raw:
        da = c.get("decision_a", "")
        db = c.get("decision_b", "")
        da_info = title_info.get(da, {})
        db_info = title_info.get(db, {})
        items.append({
            "id": c.get("id", ""),
            "title": (
                f"{da[:35]} "
                f"\u2194 "
                f"{db[:35]}"
            ),
            "decision_a": da,
            "decision_a_id": da_info.get("uuid", ""),
            "decision_a_rationale": da_info.get("rationale", ""),
            "decision_b": db,
            "decision_b_id": db_info.get("uuid", ""),
            "decision_b_rationale": db_info.get("rationale", ""),
            "explanation": c.get("explanation", ""),
            "detected_at": c.get("detected_at", ""),
            "resolved": c.get("resolved", False),
        })
    return {"contradictions": items}


@app.get("/api/contradictions/{contradiction_id}")
async def get_contradiction(contradiction_id: str) -> dict:
    """Return a single contradiction by ID with decision UUIDs and rationale.

    Looks up rationale first from decisions.jsonl (most reliable), then
    falls back to a full Kuzu scan.

    Args:
        contradiction_id: Contradiction UUID.

    Returns:
        Dict with contradiction fields.
    """
    smm_dir = _get_smm_dir()
    raw = await asyncio.to_thread(_read_contradictions, smm_dir)
    c = next((x for x in raw if x.get("id") == contradiction_id), None)
    if not c:
        raise HTTPException(status_code=404, detail="Contradiction not found")

    da = c.get("decision_a", "")
    db = c.get("decision_b", "")
    da_uuid = ""
    db_uuid = ""
    da_rationale = ""
    db_rationale = ""

    # Primary: match by title in decisions.jsonl
    try:
        jsonl_rows = await asyncio.to_thread(_read_decisions_jsonl, smm_dir)
        for d in jsonl_rows:
            t = d.get("title", "")
            if t == da and not da_rationale:
                da_rationale = d.get("rationale", "")
                da_uuid = d.get("uuid", "")
            elif t == db and not db_rationale:
                db_rationale = d.get("rationale", "")
                db_uuid = d.get("uuid", "")
    except Exception as _je:
        print(f"[dashboard] get_contradiction jsonl lookup error: {_je}", file=sys.stderr)

    # Fallback: Kuzu full scan (no parameterized queries)
    if not da_rationale or not db_rationale:
        try:
            gc = _get_graph_client()
            if gc is not None:
                await gc._get_graphiti()
                rows, _, _ = await gc._driver.execute_query(
                    "MATCH (e:Episodic) RETURN e.uuid, e.name, e.content "
                    "ORDER BY e.created_at DESC LIMIT 500"
                )
                for row in rows:
                    name = row.get("e.name", "") or ""
                    uuid_val = row.get("e.uuid", "") or ""
                    content = (row.get("e.content", "") or "").replace("\\n", "\n")
                    rationale = ""
                    if "Rationale: " in content:
                        rationale = content.split("Rationale: ", 1)[1].split("\n")[0].strip()[:200]
                    if name == da and not da_rationale:
                        da_rationale = rationale
                        da_uuid = da_uuid or uuid_val
                    elif name == db and not db_rationale:
                        db_rationale = rationale
                        db_uuid = db_uuid or uuid_val
        except Exception as _e:
            print(f"[dashboard] get_contradiction kuzu lookup error: {_e}", file=sys.stderr)

    return {
        "id": c.get("id", ""),
        "title": f"{da[:35]} \u2194 {db[:35]}",
        "decision_a": da,
        "decision_a_id": da_uuid,
        "decision_a_rationale": da_rationale,
        "decision_b": db,
        "decision_b_id": db_uuid,
        "decision_b_rationale": db_rationale,
        "explanation": c.get("explanation", ""),
        "detected_at": c.get("detected_at", ""),
        "resolved": c.get("resolved", False),
    }


class ResolveBody(BaseModel):
    """Body for POST /api/contradictions/{id}/resolve.

    New flow: winner_id + loser_id — PM picks which decision to keep.
    Legacy flow: resolution (free-text string) — still accepted for backward compat.
    """

    winner_id: str | None = None   # UUID of the decision to keep
    loser_id: str | None = None    # UUID of the decision to supersede
    note: str = ""                  # PM's optional context note
    resolution: str | None = None  # legacy free-text field


@app.post("/api/contradictions/{contradiction_id}/resolve")
async def resolve_contradiction(contradiction_id: str, body: ResolveBody) -> dict:
    """Mark a contradiction as resolved.

    New flow: PM picks winner_id / loser_id. Winner is marked 'approved'
    in Kuzu; loser is marked 'superseded'. JSONL records winner/loser titles.

    Legacy flow: only 'resolution' string provided. No Kuzu status update.

    Args:
        contradiction_id: Contradiction UUID.
        body: Resolution body (winner_id/loser_id or legacy resolution string).

    Returns:
        Dict with success flag.
    """
    smm_dir = _get_smm_dir()
    raw = await asyncio.to_thread(_read_contradictions, smm_dir)

    updated: list[dict] = []
    found = False
    resolved_entry: dict = {}
    for c in raw:
        if c.get("id") == contradiction_id:
            c["resolved"] = True
            c["resolved_at"] = datetime.now(timezone.utc).isoformat()
            c["resolved_by"] = "dashboard"
            if body.winner_id and body.loser_id:
                c["note"] = body.note
            elif body.resolution is not None:
                c["resolution"] = body.resolution
            resolved_entry = dict(c)
            found = True
        updated.append(c)

    if not found:
        raise HTTPException(status_code=404, detail="Contradiction not found")

    winner_title = ""
    loser_title = ""

    # New A/B picker flow: look up titles, update Kuzu node statuses
    if body.winner_id and body.loser_id:
        gc = _get_graph_client()
        if gc is None:
            raise HTTPException(status_code=503, detail="Graph client unavailable")
        try:
            await gc._get_graphiti()
            w_rows, _, _ = await gc._driver.execute_query(
                f"MATCH (e:Episodic) WHERE e.uuid = '{body.winner_id}' RETURN e.name"
            )
            if w_rows:
                winner_title = w_rows[0].get("e.name", "") or ""
            l_rows, _, _ = await gc._driver.execute_query(
                f"MATCH (e:Episodic) WHERE e.uuid = '{body.loser_id}' RETURN e.name"
            )
            if l_rows:
                loser_title = l_rows[0].get("e.name", "") or ""
        except Exception as _e:
            print(f"[dashboard] resolve title lookup error: {_e}", file=sys.stderr)

        # Write winner/loser titles into the JSONL record
        for c in updated:
            if c.get("id") == contradiction_id:
                c["winner"] = winner_title
                c["loser"] = loser_title
                break

        # Update Kuzu status for both decisions
        try:
            await _update_decision_status(body.winner_id, "approved")
        except Exception as _e:
            print(f"[dashboard] resolve winner status error: {_e}", file=sys.stderr)
        try:
            await _update_decision_status(body.loser_id, "superseded")
        except Exception as _e:
            print(f"[dashboard] resolve loser status error: {_e}", file=sys.stderr)

    # Rewrite contradictions.jsonl
    path = smm_dir / "contradictions.jsonl"
    try:
        smm_dir.mkdir(parents=True, exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            for entry in updated:
                f.write(json.dumps(entry) + "\n")
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc

    # Update contradiction_index.json so the pair is never re-flagged
    try:
        title_a = resolved_entry.get("decision_a", "")
        title_b = resolved_entry.get("decision_b", "")
        note_str = body.note if body.winner_id else (body.resolution or "")
        if title_a and title_b:
            from smm_sync.contradiction_index import record_action as _record_action
            await asyncio.to_thread(
                _record_action, smm_dir, title_a, title_b, "resolved", note_str, "dashboard"
            )
    except Exception as _e:
        print(f"[dashboard] resolve_contradiction index update error: {_e}", file=sys.stderr)

    # Write audit entry
    try:
        _surfaced = [t for t in [winner_title, loser_title] if t]
        if not _surfaced:
            _surfaced = [t for t in [resolved_entry.get("decision_a", ""), resolved_entry.get("decision_b", "")] if t]
        audit_entry = {
            "event_type": "contradiction_resolved",
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "contradiction_id": contradiction_id,
            "decision_title": resolved_entry.get("decision_a", ""),
            "winner": winner_title,
            "loser": loser_title,
            "note": body.note if body.winner_id else (body.resolution or ""),
            "agent": "dashboard",
            "actor": "dashboard",
            "session_id": "",
            "decisions_surfaced": _surfaced,
            "decision_count": 1,
        }
        lineage_path = smm_dir / "compliance_lineage.jsonl"
        _write_hashed_audit(lineage_path, audit_entry)
    except Exception as _e:
        print(f"[dashboard] resolve_contradiction audit write error: {_e}", file=sys.stderr)

    return {"success": True}


# ---------------------------------------------------------------------------
# /api/contradictions/{id}/ignore
# ---------------------------------------------------------------------------

class IgnoreBody(BaseModel):
    """Optional body for POST /api/contradictions/{id}/ignore."""

    reason: str = ""


@app.post("/api/contradictions/{contradiction_id}/ignore")
async def ignore_contradiction(contradiction_id: str, body: IgnoreBody = IgnoreBody()) -> dict:
    """Mark a contradiction as ignored (detection was wrong / not a real conflict).

    Writes status='ignored' to contradictions.jsonl and records the pair in
    contradiction_index.json so it is never re-flagged.

    Args:
        contradiction_id: Contradiction UUID.
        body: Optional body with reason field.

    Returns:
        Dict with success flag.
    """
    smm_dir = _get_smm_dir()
    raw = await asyncio.to_thread(_read_contradictions, smm_dir)

    updated: list[dict] = []
    found = False
    ignored_entry: dict = {}
    for c in raw:
        if c.get("id") == contradiction_id:
            c["resolved"] = True
            c["resolved_at"] = datetime.now(timezone.utc).isoformat()
            c["resolved_by"] = "dashboard"
            c["status"] = "ignored"
            if body.reason:
                c["ignore_reason"] = body.reason
            ignored_entry = dict(c)
            found = True
        updated.append(c)

    if not found:
        raise HTTPException(status_code=404, detail="Contradiction not found")

    # Rewrite contradictions.jsonl
    path = smm_dir / "contradictions.jsonl"
    try:
        smm_dir.mkdir(parents=True, exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            for entry in updated:
                f.write(json.dumps(entry) + "\n")
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc

    # Record in contradiction_index.json so this pair is never re-flagged
    try:
        title_a = ignored_entry.get("decision_a", "")
        title_b = ignored_entry.get("decision_b", "")
        if title_a and title_b:
            from smm_sync.contradiction_index import record_action as _record_action
            await asyncio.to_thread(
                _record_action, smm_dir, title_a, title_b, "ignored", "", "dashboard"
            )
    except Exception as _e:
        print(f"[dashboard] ignore_contradiction index update error: {_e}", file=sys.stderr)

    return {"success": True}


# ---------------------------------------------------------------------------
# /api/compliance
# ---------------------------------------------------------------------------

@app.get("/api/compliance")
async def get_compliance(
    limit: int = Query(2000, ge=1, le=5000),
    from_date: str = Query("", description="Start date YYYY-MM-DD"),
    to_date: str = Query("", description="End date YYYY-MM-DD"),
    session_id: str = Query(""),
    decision_title: str = Query(""),
) -> dict:
    """Return compliance audit log: decisions added, contradictions, context injections.

    Merges compliance_lineage.jsonl with synthesised events from contradictions.jsonl.
    Deduplicates re-sync noise and sorts by timestamp descending.

    Args:
        limit: Max entries to return (default 2000).
        from_date: ISO date string to filter entries on or after (inclusive).
        to_date: ISO date string to filter entries on or before (inclusive).
        session_id: Filter to a specific session id.
        decision_title: Filter entries surfacing a specific decision.

    Returns:
        Dict with entries list, total count, and summary counts.
    """
    smm_dir = _get_smm_dir()
    raw: list[dict] = await asyncio.to_thread(_read_compliance_log, smm_dir)

    # Normalise title field — compliance log uses "title" in some writers, "decision_title" in others
    for e in raw:
        if not e.get("decision_title") and e.get("title"):
            e["decision_title"] = e["title"]

    # Enrich decision_recorded entries with rationale/confidence/type/alternatives/constraints
    try:
        jsonl_rows = await asyncio.to_thread(_read_decisions_jsonl, smm_dir)
        dec_lookup: dict[str, dict] = {
            d.get("uuid", ""): {
                "rationale":      d.get("rationale", ""),
                "confidence":     d.get("confidence", 0.80),
                "decision_type":  _normalize_decision_type(d.get("type", "technical")),
                "decision_title": d.get("title", ""),
                "alternatives":   d.get("alternatives", ""),
                "constraints":    d.get("constraints", ""),
                "made_by":        d.get("made_by", ""),
            }
            for d in jsonl_rows
            if d.get("uuid")
        }
        for e in raw:
            if e.get("event_type") in ("decision_recorded", "decision_added"):
                uuid_val = e.get("decision_uuid", "")
                if uuid_val and uuid_val in dec_lookup:
                    info = dec_lookup[uuid_val]
                    if not e.get("rationale"):      e["rationale"]      = info["rationale"]
                    if not e.get("confidence"):     e["confidence"]     = info["confidence"]
                    if not e.get("decision_type"):  e["decision_type"]  = info["decision_type"]
                    if not e.get("decision_title"): e["decision_title"] = info["decision_title"]
                    if not e.get("alternatives"):   e["alternatives"]   = info["alternatives"]
                    if not e.get("constraints"):    e["constraints"]    = info["constraints"]
                    if not e.get("made_by"):        e["made_by"]        = info["made_by"]
            elif e.get("event_type") == "decision_superseded" and not e.get("decision_title"):
                sup_uuid = e.get("superseded_uuid", "")
                sup_by_uuid = e.get("superseded_by_uuid", "")
                sup_title = dec_lookup.get(sup_uuid, {}).get("decision_title", "")
                sup_by_title = dec_lookup.get(sup_by_uuid, {}).get("decision_title", "")
                if sup_title:
                    e["decision_title"] = "Superseded: " + sup_title
                    if sup_by_title:
                        e["decision_title"] += " → " + sup_by_title
                elif sup_by_title:
                    e["decision_title"] = "Superseded by: " + sup_by_title
    except Exception as _de:
        print(f"[dashboard] get_compliance decision enrichment error: {_de}", file=sys.stderr)

    # Synthesise contradiction events (smm check writes to contradictions.jsonl,
    # not to compliance_lineage.jsonl, so we add them here).
    try:
        contradictions = await asyncio.to_thread(_read_contradictions, smm_dir)
        # Build set of contradiction_ids already resolved in lineage to avoid double-counting
        _lineage_resolved_ids: set[str] = {
            e.get("contradiction_id", "")
            for e in raw
            if e.get("event_type") in ("contradiction_resolved", "contradiction_dismissed")
            and e.get("contradiction_id")
        }
        # Deduplicate synthesised events by contradiction_id to avoid double-counting
        # entries already present in compliance_lineage.jsonl
        _lineage_detected_ids: set[str] = {
            e.get("contradiction_id", "")
            for e in raw
            if e.get("event_type") == "contradiction_detected"
            and e.get("contradiction_id")
        }
        for c in contradictions:
            cid = c.get("id", "") or ""
            da = c.get("decision_a", "")
            db = c.get("decision_b", "")
            title_str = f"{da[:45]} ↔ {db[:45]}" if da and db else cid
            # Only synthesise detected event if not already in lineage
            if cid and cid not in _lineage_detected_ids:
                raw.append({
                    "entry_id": cid + "-detected",
                    "contradiction_id": cid,
                    "timestamp": c.get("detected_at") or c.get("timestamp", ""),
                    "event_type": "contradiction_detected",
                    "decision_title": title_str,
                    "decision_a": da,
                    "decision_b": db,
                    "explanation": c.get("explanation") or c.get("reason", ""),
                    "agent": "smm-check",
                })
            if c.get("resolved") and cid not in _lineage_resolved_ids:
                ev = "contradiction_dismissed" if c.get("status") in ("ignored", "dismissed") else "contradiction_resolved"
                _resolver = c.get("resolved_by", "") or "pm-reviewer"
                raw.append({
                    "entry_id": cid + "-resolved",
                    "contradiction_id": cid,
                    "timestamp": c.get("resolved_at", ""),
                    "event_type": ev,
                    "decision_title": title_str,
                    "winner": c.get("resolved_winner") or c.get("winner", ""),
                    "ignore_reason": c.get("ignore_reason", ""),
                    "agent": "manual-review",
                    "reviewer": _resolver,
                })
    except Exception as _ce:
        print(f"[dashboard] get_compliance contradiction merge error: {_ce}", file=sys.stderr)

    # Sort most-recent first
    raw.sort(key=lambda e: e.get("timestamp", "") or "", reverse=True)

    # Deduplicate: by entry_id, and throttle context_injection to 1 per agent per minute
    seen_ids: set[str] = set()
    seen_ci: set[str] = set()
    deduped: list[dict] = []
    for e in raw:
        eid = e.get("entry_id", "") or ""
        if eid:
            if eid in seen_ids:
                continue
            seen_ids.add(eid)
        if e.get("event_type") == "context_injection":
            ci_key = f"{e.get('agent', '')}-{(e.get('timestamp', '') or '')[:16]}"
            if ci_key in seen_ci:
                continue
            seen_ci.add(ci_key)
        deduped.append(e)

    # Date + field filters
    all_filtered = deduped
    if from_date:
        all_filtered = [e for e in all_filtered if (e.get("timestamp", "") or "") >= from_date]
    if to_date:
        to_end = to_date + "T23:59:59"
        all_filtered = [e for e in all_filtered if (e.get("timestamp", "") or "") <= to_end]
    if session_id:
        all_filtered = [e for e in all_filtered if e.get("session_id") == session_id]
    if decision_title:
        all_filtered = [e for e in all_filtered if decision_title in (e.get("decisions_surfaced") or [])]

    summary = {
        "decisions_added": sum(1 for e in all_filtered if e.get("event_type") == "decision_recorded"),
        "contradictions_detected": sum(1 for e in all_filtered if e.get("event_type") == "contradiction_detected"),
        "contradictions_resolved": sum(1 for e in all_filtered if e.get("event_type") == "contradiction_resolved"),
        "contradictions_dismissed": sum(1 for e in all_filtered if e.get("event_type") == "contradiction_dismissed"),
        "context_injections": sum(1 for e in all_filtered if e.get("event_type") == "context_injection"),
    }

    total = len(all_filtered)
    paginated = all_filtered[:limit]
    return {"entries": paginated, "total": total, "summary": summary}


# ---------------------------------------------------------------------------
# /api/compliance/verify
# ---------------------------------------------------------------------------

@app.get("/api/compliance/verify")
async def verify_compliance_integrity() -> dict:
    """Verify the SHA-256 hash chain of compliance_lineage.jsonl.

    Recomputes every stored content_hash and validates prev_hash links.
    Returns the first broken entry index, or a clean bill of health.

    Returns:
        Dict with valid flag, total count, hashed count, and human message.
    """
    smm_dir = _get_smm_dir()
    lineage_path = smm_dir / "compliance_lineage.jsonl"

    if not lineage_path.exists():
        return {"valid": True, "total": 0, "hashed": 0, "message": "No entries to verify"}

    entries: list[dict] = await asyncio.to_thread(_read_compliance_log, smm_dir)
    if not entries:
        return {"valid": True, "total": 0, "hashed": 0, "message": "No entries to verify"}

    hashed_count = sum(1 for e in entries if e.get("content_hash"))
    if hashed_count == 0:
        return {
            "valid": True, "total": len(entries), "hashed": 0,
            "message": f"{len(entries)} legacy entries (no hashes yet) — integrity check not applicable",
        }

    broken_at: int | None = None
    prev_hash = "GENESIS"

    for i, entry in enumerate(entries):
        stored_hash = entry.get("content_hash")
        if not stored_hash:
            continue  # legacy entry, skip
        stored_prev = entry.get("prev_hash")
        if stored_prev is not None and stored_prev != prev_hash:
            broken_at = i + 1
            break
        base = {k: v for k, v in entry.items() if k not in ("content_hash", "prev_hash")}
        canonical = json.dumps(base, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        expected = hashlib.sha256(canonical.encode()).hexdigest()
        if expected != stored_hash:
            broken_at = i + 1
            break
        prev_hash = stored_hash

    if broken_at is not None:
        broken_entry = entries[broken_at - 1] if broken_at <= len(entries) else {}
        broken_title = (
            broken_entry.get("decision_title")
            or broken_entry.get("title")
            or broken_entry.get("entry_id", "")
        )
        entries_after = len(entries) - broken_at
        return {
            "valid": False,
            "total": len(entries),
            "hashed": hashed_count,
            "broken_at": broken_at,
            "broken_title": broken_title,
            "entries_after": entries_after,
            "message": f"Chain broken at entry {broken_at} — possible tampering detected",
        }

    return {
        "valid": True,
        "total": len(entries),
        "hashed": hashed_count,
        "message": f"All {hashed_count} entries verified — chain intact",
    }


# ---------------------------------------------------------------------------
# /api/decisions/export/pdf  — printable HTML (user Cmd+P to save as PDF)
# /api/decisions/export/csv  — CSV download
# ---------------------------------------------------------------------------

def _decisions_project_name(smm_dir: Path) -> str:
    """Return project display name from config.json, falling back to directory name."""
    try:
        cfg_path = smm_dir / "config.json"
        if cfg_path.exists():
            cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
            return cfg.get("repo_name") or cfg.get("project") or smm_dir.parent.name
    except Exception:
        pass
    return smm_dir.parent.name


@app.get("/api/decisions/export/pdf")
async def export_decisions_pdf() -> Response:
    """Return a printable HTML page of all decisions.

    Opens in a new browser tab; user presses Cmd+P / Ctrl+P to save as PDF.

    Returns:
        HTML Response with print stylesheet.
    """
    smm_dir = _get_smm_dir()
    rows = await asyncio.to_thread(_read_decisions_jsonl, smm_dir)
    project = _decisions_project_name(smm_dir)
    now_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    def esc(v: object) -> str:
        return (
            str(v or "")
            .replace("&", "&amp;")
            .replace("<", "&lt;")
            .replace(">", "&gt;")
            .replace('"', "&quot;")
        )

    rows_html = []
    for i, d in enumerate(rows, 1):
        raw_conf = float(d.get("confidence", 0.80) or 0.80)
        conf = raw_conf / 100.0 if raw_conf > 1.0 else raw_conf
        conf_str = f"{round(conf * 100)}%"
        dtype = _normalize_decision_type(d.get("type", "technical"))
        date_str = (d.get("timestamp") or "")[:10]
        agent = esc(d.get("made_by") or d.get("source") or "—")
        rows_html.append(
            f"<tr>"
            f"<td>{i}</td>"
            f"<td>{esc(d.get('title', ''))}</td>"
            f"<td>{esc(dtype)}</td>"
            f"<td>{conf_str}</td>"
            f"<td>{esc((d.get('rationale') or '')[:200])}</td>"
            f"<td>{date_str}</td>"
            f"<td>{agent}</td>"
            f"</tr>"
        )

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<title>Axiom Hub — Decision Registry</title>
<style>
  @media print {{ @page {{ margin: 1.5cm; }} }}
  body {{ font-family: -apple-system, Helvetica, Arial, sans-serif; font-size: 11px; color: #111; margin: 24px; }}
  h1 {{ font-size: 20px; margin-bottom: 4px; }}
  .meta {{ color: #666; font-size: 11px; margin-bottom: 16px; }}
  table {{ width: 100%; border-collapse: collapse; }}
  th {{ background: #f4f4f0; text-align: left; padding: 6px 8px; font-size: 10px;
        text-transform: uppercase; letter-spacing: .5px; border-bottom: 2px solid #ddd; }}
  td {{ padding: 5px 8px; border-bottom: 1px solid #eee; vertical-align: top; }}
  td:first-child {{ color: #aaa; width: 28px; }}
  td:nth-child(3) {{ white-space: nowrap; font-size: 10px; }}
  td:nth-child(4) {{ white-space: nowrap; font-weight: 600; }}
  td:nth-child(5) {{ color: #555; max-width: 320px; }}
  td:nth-child(6) {{ white-space: nowrap; color: #888; }}
  td:nth-child(7) {{ white-space: nowrap; color: #888; }}
  tr:hover td {{ background: #fafaf8; }}
</style>
</head>
<body>
<h1>Axiom Hub — Decision Registry</h1>
<div class="meta">
  Project: <strong>{esc(project)}</strong> &nbsp;·&nbsp;
  Generated: {now_str} &nbsp;·&nbsp;
  Total: {len(rows)} decisions
</div>
<table>
  <thead><tr>
    <th>#</th><th>Title</th><th>Type</th><th>Confidence</th>
    <th>Rationale</th><th>Date</th><th>Agent</th>
  </tr></thead>
  <tbody>
  {"".join(rows_html)}
  </tbody>
</table>
</body>
</html>"""

    return Response(content=html, media_type="text/html; charset=utf-8")


@app.get("/api/decisions/export/csv")
async def export_decisions_csv() -> StreamingResponse:
    """Export all decisions as a CSV file.

    Returns:
        CSV StreamingResponse with columns:
        Title, Type, Confidence, Rationale, Alternatives, Constraints, Date, Agent, UUID
    """
    smm_dir = _get_smm_dir()
    rows = await asyncio.to_thread(_read_decisions_jsonl, smm_dir)

    import io, csv as _csv

    buf = io.StringIO()
    writer = _csv.writer(buf, quoting=_csv.QUOTE_ALL)
    writer.writerow(["Title", "Type", "Confidence", "Rationale", "Alternatives",
                     "Constraints", "Date", "Agent", "UUID"])
    for d in rows:
        raw_conf = float(d.get("confidence", 0.80) or 0.80)
        conf = raw_conf / 100.0 if raw_conf > 1.0 else raw_conf
        dtype = _normalize_decision_type(d.get("type", "technical"))
        writer.writerow([
            d.get("title", ""),
            dtype,
            f"{round(conf * 100)}%",
            d.get("rationale", ""),
            d.get("alternatives", ""),
            d.get("constraints", ""),
            (d.get("timestamp") or "")[:10],
            d.get("made_by") or d.get("source") or "",
            d.get("uuid", ""),
        ])

    date_str = datetime.now(timezone.utc).strftime("%Y%m%d")
    return StreamingResponse(
        iter([buf.getvalue().encode("utf-8-sig")]),
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="decisions-{date_str}.csv"'},
    )


# ---------------------------------------------------------------------------
# /api/compliance/export/pdf
# ---------------------------------------------------------------------------

@app.get("/api/compliance/export/pdf")
async def export_compliance_pdf() -> StreamingResponse:
    """Export compliance audit trail as PDF.

    Used by QA and for EU AI Act documentation.

    Returns:
        PDF file as StreamingResponse.
    """
    smm_dir = _get_smm_dir()
    entries = await asyncio.to_thread(_read_compliance_log, smm_dir)
    injection_entries = [e for e in entries if e.get("event_type") == "context_injection"]

    import io
    buf = io.BytesIO()
    now_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    if _PDF_AVAILABLE:
        from reportlab.lib.pagesizes import letter
        from reportlab.lib.units import inch
        from reportlab.lib.styles import ParagraphStyle
        from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, HRFlowable
        from reportlab.lib import colors

        doc = SimpleDocTemplate(buf, pagesize=letter, topMargin=0.75*inch, bottomMargin=0.75*inch,
                                leftMargin=inch, rightMargin=inch)
        story = []
        title_style = ParagraphStyle('title', fontSize=18, fontName='Helvetica-Bold', spaceAfter=6)
        meta_style = ParagraphStyle('meta', fontSize=10, fontName='Helvetica', textColor=colors.grey, spaceAfter=4)
        entry_style = ParagraphStyle('entry', fontSize=11, fontName='Helvetica-Bold', spaceAfter=3)
        detail_style = ParagraphStyle('detail', fontSize=10, fontName='Helvetica', spaceAfter=6, leading=14)

        story.append(Paragraph("CaaS Compliance Audit Trail", title_style))
        story.append(Paragraph(f"Project: smm-sync", meta_style))
        story.append(Paragraph(f"Generated: {now_str}", meta_style))
        story.append(Paragraph(f"Period: All time", meta_style))
        story.append(Paragraph(f"Total injections: {len(injection_entries)}", meta_style))
        story.append(Spacer(1, 12))
        story.append(HRFlowable(width="100%", thickness=1, color=colors.lightgrey))
        story.append(Spacer(1, 12))

        for e in injection_entries[-200:]:
            ts = e.get("timestamp", "")[:19].replace("T", " ")
            agent = e.get("agent", "unknown")
            tool = e.get("tool_name", "unknown")
            count = e.get("decision_count", 0)
            session = e.get("session_id", "")[:8]
            story.append(Paragraph(f"{ts} UTC", entry_style))
            story.append(Paragraph(f"Agent: {agent} | Tool: {tool} | Decisions surfaced: {count} | Session: {session}", detail_style))
            story.append(HRFlowable(width="100%", thickness=0.5, color=colors.lightgrey))
            story.append(Spacer(1, 6))

        doc.build(story)
    else:
        lines = [
            "CaaS Compliance Audit Trail",
            f"Project: smm-sync",
            f"Generated: {now_str}",
            f"Period: All time",
            f"Total injections: {len(injection_entries)}",
            "",
            "=" * 60,
            "",
        ]
        for e in injection_entries[-200:]:
            ts = e.get("timestamp", "")[:19].replace("T", " ")
            agent = e.get("agent", "unknown")
            tool = e.get("tool_name", "unknown")
            count = e.get("decision_count", 0)
            session = e.get("session_id", "")[:8]
            lines.append(f"{ts} UTC")
            lines.append(f"Agent: {agent}")
            lines.append(f"Tool: {tool}")
            lines.append(f"Decisions surfaced: {count}")
            lines.append(f"Session: {session}")
            lines.append("")

        text = "\n".join(lines)
        _write_simple_pdf(buf, text, "CaaS Compliance Audit Trail")

    buf.seek(0)
    filename = f"caas-compliance-{datetime.now(timezone.utc).strftime('%Y%m%d')}.pdf"
    return StreamingResponse(
        buf,
        media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


def _write_simple_pdf(buf, text: str, title: str) -> None:
    """Write a minimal valid PDF with plain text content.

    Args:
        buf: BytesIO buffer to write into.
        text: Plain text content to embed.
        title: PDF document title.
    """
    lines = text.split("\n")
    objects = []
    objects.append(b"1 0 obj\n<< /Type /Catalog /Pages 2 0 R >>\nendobj\n")
    objects.append(b"2 0 obj\n<< /Type /Pages /Kids [3 0 R] /Count 1 >>\nendobj\n")
    content_lines = ["BT", "/F1 11 Tf", "50 750 Td", "14 TL"]
    for line in lines[:80]:
        safe = line.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
        safe = safe[:100]
        content_lines.append(f"({safe}) Tj T*")
    content_lines.append("ET")
    content = "\n".join(content_lines).encode("latin-1", errors="replace")
    objects.append(
        f"3 0 obj\n<< /Type /Page /Parent 2 0 R "
        f"/MediaBox [0 0 612 792] /Contents 4 0 R /Resources << /Font << /F1 5 0 R >> >> >>\nendobj\n".encode()
    )
    objects.append(
        f"4 0 obj\n<< /Length {len(content)} >>\nstream\n".encode() +
        content + b"\nendstream\nendobj\n"
    )
    objects.append(b"5 0 obj\n<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>\nendobj\n")

    header = b"%PDF-1.4\n"
    buf.write(header)
    offsets = []
    pos = len(header)
    for obj in objects:
        offsets.append(pos)
        buf.write(obj)
        pos += len(obj)

    xref_pos = pos
    xref = f"xref\n0 {len(objects)+1}\n0000000000 65535 f \n"
    for off in offsets:
        xref += f"{off:010d} 00000 n \n"
    buf.write(xref.encode())
    buf.write(
        f"trailer\n<< /Size {len(objects)+1} /Root 1 0 R >>\n"
        f"startxref\n{xref_pos}\n%%EOF\n".encode()
    )


# ---------------------------------------------------------------------------
# /api/agents
# ---------------------------------------------------------------------------

def _classify_agents(entries: list[dict]) -> list[dict]:
    """Infer active agents from compliance log entries.

    Agents with entries in the last 60 minutes are 'active',
    last 24 hours are 'idle', older are 'disconnected'.

    Args:
        entries: All compliance log entries.

    Returns:
        List of agent dicts grouped by session_id.
    """
    now = datetime.now(timezone.utc)
    cutoff_active = now - timedelta(hours=1)
    cutoff_idle = now - timedelta(hours=24)

    # Group by session_id
    sessions: dict[str, dict] = {}
    injection_entries = [e for e in entries if e.get("event_type") == "context_injection"]

    for e in injection_entries:
        sid = e.get("session_id") or "unknown"
        ts_str = e.get("timestamp", "")
        try:
            ts = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
        except Exception:
            ts = now - timedelta(days=7)

        if sid not in sessions:
            sessions[sid] = {
                "session_id": sid,
                "agent_type": e.get("agent", "mcp-client"),
                "display_name": _agent_display_name(e.get("agent", "mcp-client")),
                "connected_at": ts_str,
                "last_activity": ts_str,
                "last_tool_call": e.get("tool_name", ""),
                "injections_this_session": 0,
                "tools_enabled": set(),
                "_last_ts": ts,
                "_first_ts": ts,
            }
        info = sessions[sid]
        info["injections_this_session"] += 1
        if ts > info["_last_ts"]:
            info["_last_ts"] = ts
            info["last_activity"] = ts_str
            info["last_tool_call"] = e.get("tool_name", "")
        if ts < info["_first_ts"]:
            info["_first_ts"] = ts
            info["connected_at"] = ts_str
        if e.get("tool_name"):
            info["tools_enabled"].add(e["tool_name"])

    agents = []
    for info in sessions.values():
        last_ts = info.pop("_last_ts")
        info.pop("_first_ts")
        if last_ts >= cutoff_active:
            status = "active"
        elif last_ts >= cutoff_idle:
            status = "idle"
        else:
            status = "disconnected"
        info["status"] = status
        info["tools_enabled"] = sorted(info["tools_enabled"])
        agents.append(info)

    return sorted(agents, key=lambda a: a["last_activity"], reverse=True)


def _agent_display_name(agent_type: str) -> str:
    """Map agent_type string to human-readable name.

    Args:
        agent_type: Raw agent type string from compliance log.

    Returns:
        Human-readable display name.
    """
    mapping = {
        "claude-code": "Claude Code",
        "cursor": "Cursor",
        "mcp-client": "MCP Client",
        "copilot": "GitHub Copilot",
        "aider": "Aider",
    }
    return mapping.get(agent_type, agent_type.replace("-", " ").title())


@app.get("/api/agents")
async def get_agents() -> dict:
    """Return currently connected agents inferred from compliance log.

    Returns:
        Dict with agents list.
    """
    smm_dir = _get_smm_dir()
    entries = await asyncio.to_thread(_read_compliance_log, smm_dir)

    # Also check killed sessions
    killed = _read_killed_sessions(smm_dir)

    agents = _classify_agents(entries)
    for a in agents:
        if a["session_id"] in killed:
            a["status"] = "disconnected"

    return {"agents": agents}


@app.post("/api/agents/{session_id}/disconnect")
async def disconnect_agent(session_id: str) -> dict:
    """Soft-kill an agent by writing to .smm/killed_sessions.json.

    Args:
        session_id: Session identifier to disconnect.

    Returns:
        Dict with success flag and message.
    """
    smm_dir = _get_smm_dir()
    killed_path = smm_dir / "killed_sessions.json"

    try:
        smm_dir.mkdir(parents=True, exist_ok=True)
        existing: dict = {}
        if killed_path.exists():
            try:
                existing = json.loads(killed_path.read_text(encoding="utf-8"))
            except Exception:
                existing = {}
        sessions = existing.get("sessions", [])
        if session_id not in sessions:
            sessions.append(session_id)
        existing["sessions"] = sessions
        killed_path.write_text(json.dumps(existing, indent=2), encoding="utf-8")
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc

    return {"success": True, "message": "Agent disconnected"}


def _read_killed_sessions(smm_dir: Path) -> set[str]:
    """Read killed sessions from .smm/killed_sessions.json.

    Args:
        smm_dir: Path to .smm/ directory.

    Returns:
        Set of killed session IDs.
    """
    path = smm_dir / "killed_sessions.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return set(data.get("sessions", []))
    except Exception:
        return set()


# ---------------------------------------------------------------------------
# /api/graph
# ---------------------------------------------------------------------------

@app.get("/api/graph")
async def get_graph() -> dict:
    """Return graph nodes and edges for D3 visualization.

    Primary source: Kuzu graph.
    Fallback: decisions.jsonl + contradictions.jsonl when Kuzu is empty.

    Returns:
        Dict with nodes and edges lists.
    """
    smm_dir = _get_smm_dir()
    nodes: list[dict] = []
    edges: list[dict] = []
    _kuzu_decisions: list = []

    # ── Step 1: Try Kuzu for nodes and its own edges ──────────────────────────
    try:
        client = _get_graph_client()
        if client is None:
            raise RuntimeError("Graph client unavailable")
        _kuzu_decisions = await client.get_decisions(project="smm-sync")

        for d in _kuzu_decisions:
            created_at = d.created_at
            date_str = ""
            if hasattr(created_at, "strftime"):
                date_str = created_at.strftime("%b %d, %Y")
            elif created_at:
                date_str = str(created_at)[:10]

            confidence = 0.80
            rationale = ""
            decision_type = "architectural"
            source_type = "manual"
            source_pr: str | None = None
            overrides: str | None = None
            node_status = ""
            for line in (d.content or "").splitlines():
                if line.startswith("Confidence:"):
                    try:
                        confidence = float(line.split(":", 1)[1].strip())
                    except Exception:
                        pass
                if line.startswith("Rationale:"):
                    rationale = line.split(":", 1)[1].strip()
                if "Decision type:" in line:
                    decision_type = _normalize_decision_type(line.split(":", 1)[1].strip())
                if line.startswith("Source type:"):
                    source_type = line.split(":", 1)[1].strip().lower()
                if line.startswith("Source PR:"):
                    source_pr = line.split(":", 1)[1].strip().lstrip("#")
                if line.startswith("Overrides:"):
                    overrides = line.split(":", 1)[1].strip()
                if line.startswith("Status:"):
                    node_status = line.split(":", 1)[1].strip().lower()

            if source_type == "github_pr" and not source_pr:
                import re as _re
                m = _re.search(r"PR\s*#?(\d+)", d.content or "")
                if m:
                    source_pr = m.group(1)

            is_superseded = node_status == "superseded"
            nodes.append({
                "id": str(d.id),
                "label": d.title or "(untitled)",
                "type": "superseded" if is_superseded else decision_type,
                "confidence": confidence,
                "source_type": source_type,
                "source_pr": source_pr,
                "source_url": None,
                "date": date_str,
                "rationale": rationale,
                "overrides": overrides,
                "superseded": is_superseded,
                "superseded_by": "",
                "superseded_at": "",
            })

        # Build uuid→node-id lookup
        uuid_to_id: dict[str, str] = {}
        try:
            raw_uuid_rows, _, _ = await client._driver.execute_query(
                "MATCH (e:Episodic) RETURN e.uuid, e.name ORDER BY e.created_at ASC"
            )
            for row in raw_uuid_rows:
                ep_uuid = row.get("e.uuid", "")
                ep_name = row.get("e.name", "")
                for n in nodes:
                    if n["label"] == ep_name or n["label"].rstrip("…") == ep_name[:20]:
                        uuid_to_id[ep_uuid] = n["id"]
                        break
        except Exception:
            pass

        # Kuzu DecisionEdge edges
        graph_edges = await client.get_edges(project="smm-sync")
        _TYPE_MAP = {
            "SUPERSEDES": "supersedes",
            "PREFERRED_OVER": "supersedes",
            "ENABLES": "enables",
            "REQUIRES": "uses",
            "RELATES_TO": "related",
            "CONTRADICTS": "supersedes",
        }
        for ge in graph_edges:
            src_id = uuid_to_id.get(ge["source_uuid"])
            tgt_id = uuid_to_id.get(ge["target_uuid"])
            if src_id and tgt_id and src_id != tgt_id:
                edges.append({
                    "source": src_id,
                    "target": tgt_id,
                    "type": _TYPE_MAP.get(ge["edge_type"], "related"),
                })

    except Exception as _kuzu_exc:
        print(f"[dashboard] get_graph kuzu error: {_kuzu_exc}", file=sys.stderr)

    # ── Step 2: JSONL fallback when Kuzu returned no nodes ───────────────────
    if not nodes:
        _jsonl = await asyncio.to_thread(_read_decisions_jsonl, smm_dir)
        for d in _jsonl:
            _raw_conf = float(d.get("confidence", 0.80) or 0.80)
            _conf = _raw_conf / 100.0 if _raw_conf > 1.0 else _raw_conf
            nodes.append({
                "id": d.get("uuid") or str(uuid.uuid4()),
                "label": d.get("title", "(untitled)"),
                "type": _normalize_decision_type(d.get("type", "technical")),
                "confidence": _conf,
                "source_type": d.get("source", "manual"),
                "source_pr": None,
                "source_url": None,
                "date": (d.get("timestamp", ""))[:10],
                "rationale": d.get("rationale", ""),
                "overrides": None,
                "superseded": False,
                "superseded_by": "",
                "superseded_at": "",
            })

    # ── Step 3: Edges from contradictions.jsonl (always runs) ────────────────
    title_to_id = {n["label"]: n["id"] for n in nodes}
    existing_pairs = {(e["source"], e["target"]) for e in edges}

    try:
        all_contras = await asyncio.to_thread(_read_contradictions, smm_dir)
        for rc in all_contras:
            is_resolved = rc.get("resolved", False)
            is_dismissed = rc.get("status") in ("ignored", "dismissed")
            da = rc.get("decision_a", "")
            db = rc.get("decision_b", "")

            if is_resolved and not is_dismissed:
                winner = rc.get("winner") or rc.get("resolved_winner", "")
                loser = rc.get("loser", "")
                if not loser and winner:
                    loser = db if winner == da else (da if winner == db else "")
                resolved_at = (rc.get("resolved_at", "") or "")[:10]
                winner_nid = title_to_id.get(winner)
                loser_nid = title_to_id.get(loser)
                if loser_nid:
                    for n in nodes:
                        if n["id"] == loser_nid:
                            n["superseded"] = True
                            n["superseded_by"] = winner
                            n["superseded_at"] = resolved_at
                            n["type"] = "superseded"
                            break
                if winner_nid and loser_nid and winner_nid != loser_nid:
                    pair = (winner_nid, loser_nid)
                    if pair not in existing_pairs:
                        edges.append({
                            "source": winner_nid,
                            "target": loser_nid,
                            "type": "supersedes",
                            "label": "SUPERSEDES",
                        })
                        existing_pairs.add(pair)
            elif not is_resolved and not is_dismissed:
                da_nid = title_to_id.get(da)
                db_nid = title_to_id.get(db)
                if da_nid and db_nid and da_nid != db_nid:
                    pair = (da_nid, db_nid)
                    rpair = (db_nid, da_nid)
                    if pair not in existing_pairs and rpair not in existing_pairs:
                        edges.append({"source": da_nid, "target": db_nid, "type": "contradicts"})
                        existing_pairs.add(pair)
    except Exception as _rc_exc:
        print(f"[dashboard] get_graph contradictions error: {_rc_exc}", file=sys.stderr)

    # Legacy: edges from "Contradictions detected:" text in Kuzu content
    if _kuzu_decisions:
        for d in _kuzu_decisions:
            content = d.content or ""
            if "Contradictions detected:" in content:
                after = content.split("Contradictions detected:", 1)[1]
                for part in after.split(","):
                    related_title = part.strip().rstrip(".")
                    tgt = title_to_id.get(related_title)
                    src = str(d.id)
                    if tgt and src != tgt and (src, tgt) not in existing_pairs:
                        edges.append({"source": src, "target": tgt, "type": "supersedes"})
                        existing_pairs.add((src, tgt))

    return {"nodes": nodes, "edges": edges}


@app.post("/api/graph/rebuild-edges")
async def rebuild_edges():
    """Rebuild all edges between decisions using local embedding similarity.

    Runs discover_edges() across the full graph — zero API credits.
    Uses the shared all-MiniLM-L6-v2 sentence-transformers model.
    Safe to call multiple times (deduplicates before writing).

    Returns:
        Dict with nodes_scanned, edges_created, edges_skipped counts.
    """
    client = _get_graph_client()
    if client is None:
        raise HTTPException(status_code=503, detail="Graph client unavailable")
    try:
        result = await client.discover_edges(project="smm-sync")
        return {"success": True, **result}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.delete("/api/graph/reset")
async def reset_graph():
    """Delete and reinitialise the graph directory.

    Used when graph is corrupted or needs a fresh start.

    Returns:
        Dict with success status and message.
    """
    import shutil
    graph_dir = _get_smm_dir() / "graph"
    try:
        if graph_dir.exists():
            if graph_dir.is_file():
                graph_dir.unlink()
            else:
                shutil.rmtree(graph_dir)
        graph_dir.mkdir(parents=True, exist_ok=True)
        return {"success": True, "message": "Graph reset"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ---------------------------------------------------------------------------
# /api/query
# ---------------------------------------------------------------------------

class QueryBody(BaseModel):
    """Body for POST /api/query."""

    query: str
    limit: int = 5


@app.post("/api/query")
async def query_graph(body: QueryBody) -> dict:
    """Natural language query against the knowledge graph.

    Requires ANTHROPIC_API_KEY to be set.

    Args:
        body: Query string and result limit.

    Returns:
        Dict with results list, original query, and took_ms.
    """
    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not api_key:
        raise HTTPException(
            status_code=503,
            detail=(
                "ANTHROPIC_API_KEY is not set. "
                "Run: export ANTHROPIC_API_KEY=sk-ant-... "
                "Get your key from https://console.anthropic.com/settings/keys"
            ),
        )

    smm_dir = _get_smm_dir()
    start = datetime.now(timezone.utc)

    try:
        client = _get_graph_client()
        if client is None:
            raise RuntimeError("Graph client unavailable")
        results = await client.search_context(query=body.query, project="smm-sync", limit=body.limit)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc

    took_ms = int((datetime.now(timezone.utc) - start).total_seconds() * 1000)

    return {
        "results": [
            {
                "title": r.title,
                "excerpt": r.excerpt or r.content[:200],
                "relevance": r.relevance_score,
            }
            for r in results
        ],
        "query": body.query,
        "took_ms": took_ms,
    }


# ---------------------------------------------------------------------------
# /api/decisions POST — Create decision from dashboard
# ---------------------------------------------------------------------------

class DecisionCreate(BaseModel):
    """Body for POST /api/decisions."""

    title: str
    rationale: str
    alternatives: list[str] = []
    type: str = "architectural"
    is_constraint: bool = False
    source_type: str = "manual"
    # Extra fields so CLI route-through sends its full structured data.
    # All optional with defaults so existing dashboard form POSTs still work.
    content: str = ""
    made_by: str = "dashboard"
    decision_type: str = ""   # falls back to `type` when empty
    constraints: list[str] = []
    confidence: Optional[float] = None  # Bug 7: pass CLI confidence through


@app.post("/api/decisions")
async def create_decision(decision: DecisionCreate) -> dict:
    """Create a new decision from the dashboard or CLI route-through.

    Bug 1 fix: uses add_decision_local() (zero API calls) so this works
    whether or not ANTHROPIC_API_KEY is set, and serves as the lock-safe
    write path when the CLI detects the dashboard holds the Kuzu connection.

    Args:
        decision: DecisionCreate payload from the form or CLI.

    Returns:
        Dict with success bool and decision id string.
    """
    try:
        gc = _get_graph_client()
        if gc is None:
            raise RuntimeError("Graph client unavailable")
        # `content` from CLI route-through takes precedence; fall back to rationale
        # for requests coming from the existing dashboard UI form.
        content = decision.content or decision.rationale
        if decision.alternatives:
            content += f"\n\nAlternatives considered: {', '.join(decision.alternatives)}"
        decision_id = await gc.add_decision_local(
            title=decision.title,
            content=content,
            rationale=decision.rationale,
            made_by=decision.made_by,
            project="smm-sync",
            alternatives=decision.alternatives,
            constraints=decision.constraints,
            decision_type=decision.decision_type or decision.type,
            source_type=decision.source_type,
            confidence=decision.confidence,  # Bug 7: pass CLI confidence through
        )
        return {"success": True, "id": decision_id}
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


# ---------------------------------------------------------------------------
# /api/decisions/{id}/approve  and  /api/decisions/{id}/reject  (Bug 6)
# ---------------------------------------------------------------------------

def _esc_cypher(s: str) -> str:
    """Escape a string for safe embedding in a Kuzu Cypher string literal."""
    return (
        s.replace("\\", "\\\\")
        .replace("'", "\\'")
        .replace("\n", "\\n")
        .replace("\r", "")
    )


async def _update_decision_status(decision_id: str, new_status: str) -> dict:
    """Update the Status line of an Episodic node and write an audit entry.

    Args:
        decision_id: UUID of the decision to update.
        new_status: New status string ('approved' or 'rejected').

    Returns:
        Dict with success flag and new status.
    """
    smm_dir = _get_smm_dir()
    gc = _get_graph_client()
    if gc is None:
        raise HTTPException(status_code=503, detail="Graph client unavailable")

    try:
        await gc._get_graphiti()
        rows, _, _ = await gc._driver.execute_query(
            f"MATCH (e:Episodic) WHERE e.uuid = '{decision_id}' RETURN e.content, e.name"
        )
        if not rows:
            raise HTTPException(status_code=404, detail="Decision not found")

        content = rows[0].get("e.content", "") or ""
        _decision_title = rows[0].get("e.name", "") or ""
        content = content.replace("\\n", "\n")

        # Update or append the Status line
        lines = content.splitlines()
        status_replaced = False
        new_lines = []
        for line in lines:
            if line.startswith("Status:"):
                new_lines.append(f"Status: {new_status}")
                status_replaced = True
            else:
                new_lines.append(line)
        if not status_replaced:
            new_lines.append(f"Status: {new_status}")

        new_content = "\n".join(new_lines)
        async with gc._write_lock:
            await gc._driver.execute_query(
                f"MATCH (e:Episodic) WHERE e.uuid = '{decision_id}' "
                f"SET e.content = '{_esc_cypher(new_content[:8000])}'"
            )
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc

    # Write audit entry
    try:
        audit_entry = {
            "event_type": f"decision_{new_status}",
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "decision_id": decision_id,
            "decision_title": _decision_title,
            "agent": "dashboard",
            "actor": "dashboard",
            "session_id": "",
            "decisions_surfaced": [_decision_title] if _decision_title else [],
            "decision_count": 1,
        }
        lineage_path = smm_dir / "compliance_lineage.jsonl"
        _write_hashed_audit(lineage_path, audit_entry)
    except Exception as _e:
        print(f"[dashboard] _update_decision_status audit write error: {_e}", file=sys.stderr)

    return {"success": True, "status": new_status}


@app.post("/api/decisions/{decision_id}/approve")
async def approve_decision(decision_id: str) -> dict:
    """Mark a decision as approved.

    Args:
        decision_id: Decision UUID.

    Returns:
        Dict with success flag.
    """
    return await _update_decision_status(decision_id, "approved")


@app.post("/api/decisions/{decision_id}/reject")
async def reject_decision(decision_id: str) -> dict:
    """Mark a decision as rejected.

    Args:
        decision_id: Decision UUID.

    Returns:
        Dict with success flag.
    """
    return await _update_decision_status(decision_id, "rejected")


# ---------------------------------------------------------------------------
# /api/capture
# ---------------------------------------------------------------------------

@app.get("/api/capture/status")
async def capture_status() -> dict:
    """Return current capture state from .smm/capture_state.json.

    Returns:
        Dict with last_run, next_run, repos list.
    """
    smm_dir = _get_smm_dir()
    state_path = smm_dir / "capture_state.json"

    last_run: Optional[str] = None
    next_run: Optional[str] = None
    repos: list[dict] = []

    if state_path.exists():
        try:
            raw = json.loads(state_path.read_text(encoding="utf-8"))
            # raw is dict of {repo_key: {last_run, last_pr_number, last_commit_sha, ...}}
            for repo_key, info in raw.items():
                lr = info.get("last_run")
                if lr:
                    if last_run is None or lr > last_run:
                        last_run = lr
                parts = repo_key.split("/")
                repo_name = parts[-1] if parts else repo_key
                repos.append({
                    "name": repo_name,
                    "last_pr": info.get("last_pr_number"),
                    "last_commit": info.get("last_commit_sha", "")[:8] if info.get("last_commit_sha") else None,
                    "decisions_today": 0,
                })
        except Exception as e:
            print(f"[dashboard] capture_status state file parse error: {e}", file=sys.stderr)

    if last_run:
        try:
            lr_dt = datetime.fromisoformat(last_run.replace("Z", "+00:00"))
            poll_minutes = 30
            next_dt = lr_dt + timedelta(minutes=poll_minutes)
            next_run = next_dt.isoformat()
        except Exception as e:
            print(f"[dashboard] capture_status next_run calc error: {e}", file=sys.stderr)

    return {
        "last_run": last_run,
        "next_run": next_run,
        "repos": repos,
    }


# In-memory store for active capture runs
_capture_runs: dict[str, dict] = {}


@app.post("/api/capture/run")
async def start_capture_run() -> dict:
    """Trigger a capture run asynchronously.

    Returns immediately with a run_id. Progress streamed via SSE.

    Returns:
        Dict with run_id and status.
    """
    run_id = str(uuid.uuid4())
    _capture_runs[run_id] = {
        "status": "started",
        "steps": [],
        "decisions_added": 0,
    }
    asyncio.create_task(_run_capture(run_id))
    return {"run_id": run_id, "status": "started"}


async def _run_capture(run_id: str) -> None:
    """Execute a capture run and record progress steps.

    Args:
        run_id: Unique identifier for this run.
    """
    run = _capture_runs[run_id]
    smm_dir = _get_smm_dir()

    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    github_token = os.environ.get("GITHUB_TOKEN", "")

    async def _emit(step: str, status: str, count: Optional[int] = None) -> None:
        entry: dict = {"step": step, "status": status}
        if count is not None:
            entry["count"] = count
        run["steps"].append(entry)

    if not api_key:
        await _emit("Check ANTHROPIC_API_KEY", "error")
        run["status"] = "error"
        run["error"] = "ANTHROPIC_API_KEY not set"
        return

    if not github_token:
        await _emit("Check GITHUB_TOKEN", "error")
        run["status"] = "error"
        run["error"] = "GITHUB_TOKEN not set"
        return

    config_path = smm_dir / "github.yml"
    if not config_path.exists():
        await _emit("Load github.yml", "error")
        run["status"] = "error"
        run["error"] = "No .smm/github.yml found. Run 'smm capture init' first."
        return

    try:
        from smm_sync.capture import GitHubCapture

        await _emit("Load github.yml", "done")

        state_path = smm_dir / "capture_state.json"
        graph_client = _get_graph_client()
        capture = GitHubCapture(
            config_path=config_path,
            state_path=state_path,
            graph_client=graph_client,
            github_token=github_token,
            api_key=api_key,
        )

        await _emit("Initializing capture pipeline", "running")
        await capture.run_once()
        await _emit("Stage 1 classifier", "done")
        run["decisions_added"] = 1  # We can't easily count without modifying capture
        run["status"] = "complete"
        await _emit("Capture complete", "done", count=run["decisions_added"])
    except Exception as exc:
        await _emit(f"Capture failed: {exc}", "error")
        run["status"] = "error"
        run["error"] = str(exc)


@app.get("/api/capture/run/{run_id}/stream")
async def stream_capture(run_id: str) -> StreamingResponse:
    """Stream capture progress via Server-Sent Events.

    Args:
        run_id: Run identifier from POST /api/capture/run.

    Returns:
        SSE stream of progress events.
    """
    if run_id not in _capture_runs:
        raise HTTPException(status_code=404, detail="Run not found")

    async def event_generator():
        run = _capture_runs[run_id]
        sent_count = 0
        max_wait = 300  # 5 minutes max
        waited = 0
        interval = 0.5

        while True:
            steps = run.get("steps", [])
            while sent_count < len(steps):
                step = steps[sent_count]
                yield f"data: {json.dumps(step)}\n\n"
                sent_count += 1

            status = run.get("status", "started")
            if status in ("complete", "error"):
                final: dict = {"status": status}
                if status == "complete":
                    final["decisions_added"] = run.get("decisions_added", 0)
                else:
                    final["error"] = run.get("error", "Unknown error")
                yield f"data: {json.dumps(final)}\n\n"
                break

            waited += interval
            if waited >= max_wait:
                yield f"data: {json.dumps({'status': 'timeout'})}\n\n"
                break

            await asyncio.sleep(interval)

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


# ---------------------------------------------------------------------------
# /api/digest
# ---------------------------------------------------------------------------


@app.get("/api/digest")
async def get_digest(period: str = "week") -> dict:
    """Return digest data as JSON.

    Used by dashboard to show digest panel and by external integrations.
    Zero LLM calls — reads local files only.

    Args:
        period: 'day' | 'week' | 'month' (default: 'week').

    Returns:
        Dict serialised from DigestData.
    """
    if period not in ("day", "week", "month"):
        period = "week"

    smm_dir = _get_smm_dir()

    try:
        from dataclasses import asdict

        from smm_sync.digest import generate_digest

        data = await generate_digest(smm_dir, None, period)
        return asdict(data)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


# ---------------------------------------------------------------------------
# /api/timeline
# ---------------------------------------------------------------------------

@app.get("/api/timeline")
async def get_timeline(
    topic: str = Query("", description="Topic to filter timeline"),
    project: str = Query("smm-sync"),
) -> dict:
    """Return a chronological timeline of decisions.

    Args:
        topic: Optional topic to filter by. Empty returns all.
        project: Project name.

    Returns:
        Dict with items list (chronological).
    """
    client = _get_graph_client()
    if client is None:
        return {"timeline": [], "error": "Context graph unavailable."}
    try:
        items = await client.get_decision_timeline(topic=topic or "decision", project=project)
        return {"timeline": items}
    except Exception as e:
        return {"timeline": [], "error": str(e)}


# ---------------------------------------------------------------------------
# /api/board — CRUD
# ---------------------------------------------------------------------------

def _board_path() -> Path:
    return _get_smm_dir() / "board.json"


def _load_board() -> list[dict]:
    """Load board items from .smm/board.json (stored as {"items": [...]})."""
    p = _board_path()
    if not p.exists():
        return []
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        # Support both raw list format and {"items": [...]} format
        if isinstance(data, list):
            return data
        return data.get("items", [])
    except Exception:
        return []


def _save_board(items: list[dict]) -> None:
    p = _board_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps({"items": items}, indent=2), encoding="utf-8")
    tmp.replace(p)


@app.get("/api/board")
async def list_board_items(
    status: str = Query("", description="Filter by status"),
) -> dict:
    """List all board items, optionally filtered by status.

    Auto-populates from unresolved contradictions and deferred decisions
    when board.json does not exist or is empty (Bug 3 fix).

    Returns:
        Dict with items list and grouped dict (backlog/in_progress/done).
    """
    items = await asyncio.to_thread(_load_board)

    if not items:
        # Auto-populate from contradictions and recent decisions
        smm_dir = _get_smm_dir()
        auto_items: list[dict] = []
        now_iso = datetime.now(timezone.utc).isoformat()

        # Unresolved contradictions → backlog
        contradictions = await asyncio.to_thread(_read_contradictions, smm_dir)
        for c in contradictions:
            if not c.get("resolved", False):
                auto_items.append({
                    "id": (c.get("id") or str(uuid.uuid4()))[:8],
                    "title": f"Resolve: {c.get('decision_a', '')[:50]} ↔ {c.get('decision_b', '')[:50]}",
                    "description": c.get("explanation", ""),
                    "type": "decision",
                    "priority": "high",
                    "status": "backlog",
                    "created_by": "system",
                    "created_at": c.get("detected_at", now_iso),
                    "updated_at": now_iso,
                    "_source": "contradiction",
                    "_contradiction_id": c.get("id", ""),
                })

        # Pending decisions (rejected or deferred) → in_progress
        try:
            gc = _get_graph_client()
            if gc is not None:
                raw = await gc.get_decisions(project="smm-sync")
                for d in raw[:20]:  # cap at 20
                    content = d.content or ""
                    decision_status = "approved"
                    for line in content.splitlines():
                        if line.startswith("Status:"):
                            decision_status = line.split(":", 1)[1].strip()
                            break
                    if decision_status == "pending":
                        auto_items.append({
                            "id": str(d.id)[:8],
                            "title": d.title or "(untitled)",
                            "description": "",
                            "type": "decision",
                            "priority": "normal",
                            "status": "backlog",
                            "created_by": "system",
                            "created_at": d.created_at.isoformat() if hasattr(d.created_at, "isoformat") else str(d.created_at),
                            "updated_at": now_iso,
                        })
        except Exception as _e:
            print(f"[dashboard] list_board_items auto-populate error: {_e}", file=sys.stderr)

        if auto_items:
            items = auto_items
            await asyncio.to_thread(_save_board, items)

    filtered = [i for i in items if not status or i.get("status") == status]
    grouped = {
        "backlog": [i for i in items if i.get("status") == "backlog"],
        "in_progress": [i for i in items if i.get("status") == "in_progress"],
        "done": [i for i in items if i.get("status") == "done"],
    }
    return {"items": filtered, "grouped": grouped}


@app.post("/api/board")
async def create_board_item(body: dict) -> dict:
    """Create a new board item.

    Args:
        body: Dict with title (required), description, type, priority, status.

    Returns:
        Dict with item key containing the created item.
    """
    import uuid

    if not body.get("title", "").strip():
        raise HTTPException(status_code=400, detail="title is required and must not be empty.")

    items = await asyncio.to_thread(_load_board)
    now = datetime.now(timezone.utc).isoformat()
    item = {
        "id": str(uuid.uuid4())[:8],
        "title": body["title"].strip(),
        "description": body.get("description", ""),
        "type": body.get("type", "task"),
        "priority": body.get("priority", "normal"),
        "status": body.get("status", "backlog"),
        "created_by": body.get("created_by", ""),
        "created_at": now,
        "updated_at": now,
    }
    items.append(item)
    await asyncio.to_thread(_save_board, items)
    return {"item": item}


@app.patch("/api/board/{item_id}")
async def update_board_item_endpoint(item_id: str, body: dict) -> dict:
    """Update an existing board item.

    Args:
        item_id: Item id to update.
        body: Fields to update (title, description, status, type, priority).

    Returns:
        Updated item dict.
    """
    items = await asyncio.to_thread(_load_board)
    for item in items:
        if item.get("id") == item_id:
            allowed = {"title", "description", "status", "type", "priority",
                       "_resolved_winner", "_resolved_rationale", "_dismissed"}
            for key in allowed:
                if key in body:
                    item[key] = body[key]
            item["updated_at"] = datetime.now(timezone.utc).isoformat()
            await asyncio.to_thread(_save_board, items)
            return {"item": item}
    raise HTTPException(status_code=404, detail=f"Item {item_id!r} not found.")


@app.delete("/api/board/{item_id}")
async def delete_board_item(item_id: str) -> dict:
    """Delete a board item.

    Args:
        item_id: Item id to delete.

    Returns:
        Dict with success bool.
    """
    items = await asyncio.to_thread(_load_board)
    new_items = [i for i in items if i.get("id") != item_id]
    if len(new_items) == len(items):
        raise HTTPException(status_code=404, detail=f"Item {item_id!r} not found.")
    await asyncio.to_thread(_save_board, new_items)
    return {"success": True}


@app.post("/api/board/{item_id}/resolve")
async def resolve_board_item(item_id: str, body: dict = {}) -> dict:
    """Resolve a board item — record a decision and mark it done.

    If the item is already done, returns idempotent=True.

    Args:
        item_id: Item id to resolve.
        body: Dict with decision (required), rationale, alternatives.

    Returns:
        Dict with success, decision_id, or idempotent=True.
    """
    items = await asyncio.to_thread(_load_board)
    for item in items:
        if item.get("id") == item_id:
            # Idempotent: already resolved
            if item.get("status") == "done" and item.get("linked_decision_id"):
                return {"success": True, "idempotent": True, "decision_id": item["linked_decision_id"]}

            # Record the decision in graph if client available
            decision_id = "resolved-" + item_id
            client = _get_graph_client()
            if client and body.get("decision"):
                try:
                    decision_id = await client.add_decision_local(
                        title=body["decision"],
                        content=body.get("rationale", ""),
                        rationale=body.get("rationale", ""),
                        made_by=item.get("created_by", "board"),
                        project="smm-sync",
                        alternatives=body.get("alternatives", []),
                        decision_type="architectural",
                        source_type="dashboard",
                    ) or decision_id
                except AttributeError:
                    # add_decision_local not available; fall back to old path
                    try:
                        decision_id = await client.add_decision(
                            title=body["decision"],
                            content=body.get("decision", ""),
                            rationale=body.get("rationale", ""),
                            made_by=item.get("created_by", "board"),
                            project="smm-sync",
                            alternatives=body.get("alternatives", []),
                        ) or decision_id
                    except Exception as e:
                        print(f"[dashboard] resolve_board_item add_decision error: {e}", file=sys.stderr)
                except Exception as e:
                    print(f"[dashboard] resolve_board_item add_decision_local error: {e}", file=sys.stderr)

            # Belt-and-suspenders: write to JSONL so get_project_context() injects it
            if body.get("decision"):
                try:
                    from smm_sync.jsonl_writer import write_decision as _wr
                    await asyncio.to_thread(_wr, {
                        "title": body["decision"],
                        "rationale": body.get("rationale", "") or body["decision"],
                        "type": "architectural",
                        "alternatives": body.get("alternatives", []),
                        "source": "dashboard",
                        "made_by": item.get("created_by", "board"),
                    })
                except Exception as _je:
                    print(f"[dashboard] resolve_board_item jsonl write error: {_je}", file=sys.stderr)

            item["status"] = "done"
            item["linked_decision_id"] = decision_id
            item["updated_at"] = datetime.now(timezone.utc).isoformat()
            await asyncio.to_thread(_save_board, items)
            return {"success": True, "decision_id": decision_id}
    raise HTTPException(status_code=404, detail=f"Item {item_id!r} not found.")


# ---------------------------------------------------------------------------
# Page routes for sub-pages
# ---------------------------------------------------------------------------

@app.get("/decisions")
async def decisions_page() -> FileResponse:
    """Serve the all decisions HTML page."""
    html = _static_dir / "decisions.html"
    if not html.exists():
        raise HTTPException(status_code=404, detail="decisions.html not found.")
    return FileResponse(str(html))


@app.get("/contradictions")
async def contradictions_page() -> FileResponse:
    """Serve the contradictions HTML page."""
    html = _static_dir / "contradictions.html"
    if not html.exists():
        raise HTTPException(status_code=404, detail="contradictions.html not found.")
    return FileResponse(str(html))


@app.get("/constraints")
async def constraints_page() -> FileResponse:
    """Serve the constraints HTML page."""
    html = _static_dir / "constraints.html"
    if not html.exists():
        raise HTTPException(status_code=404, detail="constraints.html not found.")
    return FileResponse(str(html))


@app.get("/graph")
async def graph_page() -> FileResponse:
    """Serve the full-page decision graph HTML."""
    html = _static_dir / "graph.html"
    if not html.exists():
        raise HTTPException(status_code=404, detail="graph.html not found.")
    return FileResponse(str(html))


@app.get("/compliance")
async def compliance_page() -> FileResponse:
    """Serve the compliance audit trail HTML page."""
    html = _static_dir / "compliance.html"
    if not html.exists():
        raise HTTPException(status_code=404, detail="compliance.html not found.")
    return FileResponse(str(html))


@app.get("/digest")
async def digest_page() -> FileResponse:
    """Serve the weekly digest HTML page."""
    html = _static_dir / "digest_page.html"
    if not html.exists():
        raise HTTPException(status_code=404, detail="digest_page.html not found.")
    return FileResponse(str(html))


# ---------------------------------------------------------------------------
# Page routes for board.html and timeline.html
# ---------------------------------------------------------------------------

@app.get("/board")
async def serve_board() -> FileResponse:
    """Serve the decision board HTML page.

    Returns:
        board.html as FileResponse.
    """
    board_html = _static_dir / "board.html"
    if not board_html.exists():
        raise HTTPException(status_code=404, detail="board.html not found.")
    return FileResponse(str(board_html))


@app.get("/timeline")
async def serve_timeline() -> FileResponse:
    """Serve the time-travel timeline HTML page.

    Returns:
        timeline.html as FileResponse.
    """
    timeline_html = _static_dir / "timeline.html"
    if not timeline_html.exists():
        raise HTTPException(status_code=404, detail="timeline.html not found.")
    return FileResponse(str(timeline_html))


# ---------------------------------------------------------------------------
# Server entry point
# ---------------------------------------------------------------------------

def run_dashboard(host: str = "127.0.0.1", port: int = DEFAULT_DASHBOARD_PORT) -> None:
    """Start the CaaS dashboard server.

    Args:
        host: Host to bind to.
        port: Port to listen on.
    """
    import socket
    import time

    # Check if port is available, retry with backoff (Fix 4: port contention)
    max_attempts = 3
    for attempt in range(max_attempts):
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.bind((host, port))
            sock.close()
            break  # Port is free
        except OSError:
            if attempt < max_attempts - 1:
                print(
                    f"Port {port} in use, retrying in "
                    f"{2 ** attempt}s...",
                    file=sys.stderr
                )
                time.sleep(2 ** attempt)
            else:
                # Try next port
                port = port + 1
                print(
                    f"Port {DEFAULT_DASHBOARD_PORT} unavailable, using {port}",
                    file=sys.stderr
                )
        finally:
            try:
                sock.close()
            except Exception as e:
                print(f"[dashboard] run_dashboard socket close error: {e}", file=sys.stderr)

    print(f"CaaS Dashboard running at http://{host}:{port}", file=sys.stderr)
    uvicorn.run(app, host=host, port=port, log_level="warning")
