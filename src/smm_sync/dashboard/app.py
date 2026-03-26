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
import json
import os
import sys
import uuid
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Optional

import uvicorn
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

try:
    from reportlab.lib.pagesizes import letter
    from reportlab.lib.units import inch
    from reportlab.pdfgen import canvas as rl_canvas
    _PDF_AVAILABLE = True
except ImportError:
    _PDF_AVAILABLE = False

@asynccontextmanager
async def _lifespan(app: FastAPI):
    """Eagerly initialise the graph client and embedding model at startup.

    Without this, the first request to any graph-backed endpoint pays the
    2-3 second sentence-transformer model load + Kuzu connection open cost.
    """
    try:
        client = _get_graph_client()
        if client is not None:
            await client._get_graphiti()
    except Exception as exc:
        print(f"[dashboard] startup preload warning: {exc}", file=sys.stderr)
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


def _get_graph_client():
    """Return (lazily initialise) the GraphClient for the context graph.

    Returns:
        GraphClient instance, or None if context_graph is unavailable.
    """
    global _graph_client_cache
    if _graph_client_cache is None:
        try:
            from smm_sync.context_graph.client import get_graph_client

            graph_dir = _get_smm_dir() / "graph"
            _graph_client_cache = get_graph_client(graph_dir=graph_dir)
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
                        items.append(json.loads(line))
                    except Exception as e:
                        print(f"[dashboard] _read_contradictions json parse error: {e}", file=sys.stderr)
    except Exception as e:
        print(f"[dashboard] _read_contradictions file read error: {e}", file=sys.stderr)
    return items


def _write_contradiction(smm_dir: Path, entry: dict) -> None:
    """Append a contradiction entry to .smm/contradictions.jsonl.

    Args:
        smm_dir: Path to .smm/ directory.
        entry: Contradiction dict to append.
    """
    path = smm_dir / "contradictions.jsonl"
    try:
        smm_dir.mkdir(parents=True, exist_ok=True)
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry) + "\n")
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

    # Count decisions added today
    decision_entries = [e for e in entries if e.get("event_type") == "decision_added"]
    captures_today = sum(1 for e in decision_entries if e.get("timestamp", "").startswith(today))

    # Avg confidence from decision_added entries
    confidences = [e.get("confidence", 0.0) for e in decision_entries if "confidence" in e]
    avg_confidence = round(sum(confidences) / len(confidences), 2) if confidences else 0.82

    # Count contradictions
    contradictions = await asyncio.to_thread(_read_contradictions, smm_dir)
    contradiction_count = sum(1 for c in contradictions if not c.get("resolved", False))

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
        # Fall back to compliance log if graph is unavailable
        decision_count = len(set(e.get("decision_title", "") for e in decision_entries if e.get("decision_title")))

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
        "decisions_this_week": captures_today,
        "time_saved_hours": total_time_saved_hours,
        "time_saved": time_saved["time_saved_formatted_total"],
        "time_saved_meta": "context lookups avoided",

        # existing time-saved fields
        **time_saved,
    }


# ---------------------------------------------------------------------------
# /api/decisions
# ---------------------------------------------------------------------------

@app.get("/api/decisions")
async def get_decisions(
    limit: int = Query(20, ge=1, le=200),
    offset: int = Query(0, ge=0),
    type: str = Query("all"),
    status: str = Query("all"),
) -> dict:
    """Return paginated list of decisions from the knowledge graph.

    Falls back to empty list if graph is unavailable (no crash on fresh install).

    Args:
        limit: Maximum results to return.
        offset: Number of results to skip.
        type: Decision type filter ('all', 'architectural', 'technical', 'product').
        status: Status filter ('all', 'approved', 'deferred', 'ignored').

    Returns:
        Dict with decisions list, total, limit, offset.
    """
    smm_dir = _get_smm_dir()
    decisions: list[dict] = []

    try:
        client = _get_graph_client()
        if client is None:
            raise RuntimeError("Graph client unavailable")
        raw = await client.get_decisions(project="smm-sync")

        for d in raw:
            decision_type = "architectural"
            content_lower = (d.content or "").lower()
            if "product" in content_lower:
                decision_type = "product"
            elif "technical" in content_lower:
                decision_type = "technical"

            if type != "all" and decision_type != type:
                continue

            # Parse status from content
            decision_status = "approved"
            for line in (d.content or "").splitlines():
                if line.startswith("Status:"):
                    decision_status = line.split(":", 1)[1].strip()
                    break

            if status != "all" and decision_status != status:
                continue

            # Parse confidence from content
            confidence = 0.80
            for line in (d.content or "").splitlines():
                if line.startswith("Confidence:"):
                    try:
                        confidence = float(line.split(":", 1)[1].strip())
                    except Exception as e:
                        print(f"[dashboard] get_decisions confidence parse error: {e}", file=sys.stderr)

            # Parse rationale from content
            rationale = ""
            for line in (d.content or "").splitlines():
                if line.startswith("Rationale:"):
                    rationale = line.split(":", 1)[1].strip()
                    break

            created_at = d.created_at
            if hasattr(created_at, "isoformat"):
                created_at_str = created_at.isoformat()
            else:
                created_at_str = str(created_at) if created_at else datetime.now(timezone.utc).isoformat()

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
        print(f"[dashboard] get_decisions error: {_exc}", file=sys.stderr)

    total = len(decisions)
    paginated = decisions[offset: offset + limit]

    return {
        "decisions": paginated,
        "total": total,
        "limit": limit,
        "offset": offset,
    }


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
    """Return list of detected contradictions.

    Returns:
        Dict with contradictions list.
    """
    smm_dir = _get_smm_dir()
    raw = await asyncio.to_thread(_read_contradictions, smm_dir)
    items = []
    for c in raw:
        items.append({
            "id": c.get("id", ""),
            "title": (
                f"{c.get('decision_a', 'Unknown')[:35]} "
                f"\u2194 "
                f"{c.get('decision_b', 'Unknown')[:35]}"
            ),
            "decision_a": c.get("decision_a", ""),
            "decision_b": c.get("decision_b", ""),
            "explanation": c.get("explanation", ""),
            "detected_at": c.get("detected_at", ""),
            "resolved": c.get("resolved", False),
        })
    return {"contradictions": items}


class ResolveBody(BaseModel):
    """Body for POST /api/contradictions/{id}/resolve."""

    resolution: str


@app.post("/api/contradictions/{contradiction_id}/resolve")
async def resolve_contradiction(contradiction_id: str, body: ResolveBody) -> dict:
    """Mark a contradiction as resolved.

    Args:
        contradiction_id: Contradiction UUID.
        body: Resolution description.

    Returns:
        Dict with success flag.
    """
    smm_dir = _get_smm_dir()
    raw = await asyncio.to_thread(_read_contradictions, smm_dir)

    updated = []
    found = False
    for c in raw:
        if c.get("id") == contradiction_id:
            c["resolved"] = True
            c["resolution"] = body.resolution
            c["resolved_at"] = datetime.now(timezone.utc).isoformat()
            found = True
        updated.append(c)

    if not found:
        raise HTTPException(status_code=404, detail="Contradiction not found")

    # Rewrite the file
    path = smm_dir / "contradictions.jsonl"
    try:
        with open(path, "w", encoding="utf-8") as f:
            for entry in updated:
                f.write(json.dumps(entry) + "\n")
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc

    return {"success": True}


# ---------------------------------------------------------------------------
# /api/compliance
# ---------------------------------------------------------------------------

@app.get("/api/compliance")
async def get_compliance(
    limit: int = Query(20, ge=1, le=500),
    session_id: str = Query(""),
    decision_title: str = Query(""),
) -> dict:
    """Return compliance lineage entries with optional filters.

    Args:
        limit: Max entries to return.
        session_id: Filter to specific session.
        decision_title: Filter to entries surfacing a specific decision.

    Returns:
        Dict with entries list and total count.
    """
    smm_dir = _get_smm_dir()
    all_entries = await asyncio.to_thread(_read_compliance_log, smm_dir)

    filtered = all_entries
    if session_id:
        filtered = [e for e in filtered if e.get("session_id") == session_id]
    if decision_title:
        filtered = [e for e in filtered if decision_title in e.get("decisions_surfaced", [])]

    total = len(filtered)
    # Return most recent first
    paginated = list(reversed(filtered))[:limit]

    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    deja_vu_count_today = sum(
        1 for e in all_entries
        if e.get("timestamp", "").startswith(today)
        and e.get("event_type") == "context_injection"
        and any(
            kw in str(e.get("decisions_surfaced", "")).lower()
            for kw in ("rejected", "alternative", "considered", "discarded")
        )
    )

    return {"entries": paginated, "total": total, "deja_vu_count_today": deja_vu_count_today}


# ---------------------------------------------------------------------------
# /api/decisions/export/pdf
# ---------------------------------------------------------------------------

@app.get("/api/decisions/export/pdf")
async def export_decisions_pdf() -> StreamingResponse:
    """Export all decisions as a formatted PDF.

    Used by QA for formal sign-off documentation.

    Returns:
        PDF file as StreamingResponse.
    """
    smm_dir = _get_smm_dir()
    entries = await asyncio.to_thread(_read_compliance_log, smm_dir)
    decision_entries = [e for e in entries if e.get("event_type") == "decision_added"]

    # Try to get decisions from graph
    decisions: list[dict] = []
    try:
        client_obj = _get_graph_client()
        if client_obj is not None:
            raw = await client_obj.get_all_decisions(project="smm-sync", limit=500)
            decisions = [
                {
                    "title": d.title,
                    "rationale": d.content or d.excerpt or "",
                    "type": getattr(d, "source_type", "architectural"),
                    "confidence": getattr(d, "relevance_score", 0.85),
                    "date": getattr(d, "created_at", ""),
                    "is_constraint": getattr(d, "is_constraint", False),
                }
                for d in (raw or [])
            ]
    except Exception as e:
        print(f"[dashboard] export_decisions_pdf graph query error: {e}", file=sys.stderr)

    if not decisions:
        decisions = [
            {
                "title": e.get("decision_title", "Unknown"),
                "rationale": "",
                "type": "captured",
                "confidence": e.get("confidence", 0.85),
                "date": e.get("timestamp", ""),
                "is_constraint": False,
            }
            for e in decision_entries
        ]

    import io
    buf = io.BytesIO()

    if _PDF_AVAILABLE:
        from reportlab.lib.pagesizes import letter
        from reportlab.lib.units import inch
        from reportlab.lib.styles import getSampleStyleSheet
        from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, HRFlowable
        from reportlab.lib.enums import TA_LEFT
        from reportlab.lib import colors
        from reportlab.lib.styles import ParagraphStyle

        doc = SimpleDocTemplate(buf, pagesize=letter, topMargin=0.75*inch, bottomMargin=0.75*inch,
                                leftMargin=inch, rightMargin=inch)
        styles = getSampleStyleSheet()
        story = []

        title_style = ParagraphStyle('title', fontSize=18, fontName='Helvetica-Bold', spaceAfter=6)
        meta_style = ParagraphStyle('meta', fontSize=10, fontName='Helvetica', textColor=colors.grey, spaceAfter=4)
        dec_title_style = ParagraphStyle('dec_title', fontSize=12, fontName='Helvetica-Bold', spaceAfter=4)
        body_style = ParagraphStyle('body', fontSize=10, fontName='Helvetica', spaceAfter=6, leading=14)
        label_style = ParagraphStyle('label', fontSize=9, fontName='Helvetica-Bold', textColor=colors.grey, spaceAfter=2)

        story.append(Paragraph("CaaS Decision Registry", title_style))
        story.append(Paragraph(f"Project: smm-sync", meta_style))
        story.append(Paragraph(f"Generated: {datetime.now(timezone.utc).strftime('%Y-%m-%d')}", meta_style))
        story.append(Paragraph(f"Total: {len(decisions)} decisions", meta_style))
        story.append(Spacer(1, 12))
        story.append(HRFlowable(width="100%", thickness=1, color=colors.lightgrey))
        story.append(Spacer(1, 12))

        for i, d in enumerate(decisions, 1):
            story.append(Paragraph(f"{i}. {d['title']}", dec_title_style))
            conf = d.get('confidence', 0)
            dtype = d.get('type', 'architectural')
            date_str = d.get('date', '')[:10] if d.get('date') else ''
            story.append(Paragraph(f"Type: {dtype} | Confidence: {conf:.2f} | Date: {date_str}", label_style))
            rationale = d.get('rationale', '')
            if rationale:
                story.append(Paragraph(rationale[:500], body_style))
            story.append(HRFlowable(width="100%", thickness=0.5, color=colors.lightgrey))
            story.append(Spacer(1, 8))

        doc.build(story)
    else:
        now_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        lines = [
            "CaaS Decision Registry",
            f"Project: smm-sync",
            f"Generated: {now_str}",
            f"Total: {len(decisions)} decisions",
            "",
            "=" * 60,
            "",
        ]
        for i, d in enumerate(decisions, 1):
            lines.append(f"{i}. {d['title']}")
            lines.append(f"   Type: {d.get('type','?')} | Confidence: {d.get('confidence',0):.2f}")
            if d.get('rationale'):
                lines.append(f"   {d['rationale'][:200]}")
            lines.append("")

        text = "\n".join(lines)
        _write_simple_pdf(buf, text, "CaaS Decision Registry")

    buf.seek(0)
    filename = f"caas-decisions-{datetime.now(timezone.utc).strftime('%Y%m%d')}.pdf"
    return StreamingResponse(
        buf,
        media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
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

    Falls back to empty graph on any error.

    Returns:
        Dict with nodes and edges lists.
    """
    smm_dir = _get_smm_dir()
    nodes: list[dict] = []
    edges: list[dict] = []

    try:
        client = _get_graph_client()
        if client is None:
            raise RuntimeError("Graph client unavailable")
        decisions = await client.get_decisions(project="smm-sync")

        for d in decisions:
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
            for line in (d.content or "").splitlines():
                if line.startswith("Confidence:"):
                    try:
                        confidence = float(line.split(":", 1)[1].strip())
                    except Exception as e:
                        print(f"[dashboard] get_graph confidence parse error: {e}", file=sys.stderr)
                if line.startswith("Rationale:"):
                    rationale = line.split(":", 1)[1].strip()
                if "Decision type:" in line:
                    decision_type = line.split(":", 1)[1].strip().lower()
                if line.startswith("Source type:"):
                    source_type = line.split(":", 1)[1].strip().lower()
                if line.startswith("Source PR:"):
                    source_pr = line.split(":", 1)[1].strip().lstrip("#")
                if line.startswith("Overrides:"):
                    overrides = line.split(":", 1)[1].strip()

            # Try to extract PR number from content if source is github_pr
            if source_type == "github_pr" and not source_pr:
                import re as _re
                m = _re.search(r"PR\s*#?(\d+)", d.content or "")
                if m:
                    source_pr = m.group(1)

            nodes.append({
                "id": str(d.id),
                "label": d.title or "(untitled)",
                "type": decision_type,
                "confidence": confidence,
                "source_type": source_type,
                "source_pr": source_pr,
                "source_url": None,  # constructed on frontend using repo_owner/repo_name
                "date": date_str,
                "rationale": rationale,
                "overrides": overrides,
            })

        # Do not filter all-uppercase labels here.
        # Legitimate decisions can have titles like "AFFECTS", and the old
        # regex removed them, causing /graph to render "No decisions yet".

        # Detect supersedes edges from content
        title_to_id = {n["label"]: n["id"] for n in nodes}
        for d in decisions:
            content = d.content or ""
            if "Contradictions detected:" in content:
                after = content.split("Contradictions detected:", 1)[1]
                for part in after.split(","):
                    related_title = part.strip().rstrip(".")
                    if related_title in title_to_id and str(d.id) != title_to_id[related_title]:
                        edges.append({
                            "source": str(d.id),
                            "target": title_to_id[related_title],
                            "type": "supersedes",
                        })
    except Exception as _exc:
        print(f"[dashboard] get_graph error: {_exc}", file=sys.stderr)

    return {"nodes": nodes, "edges": edges}


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


@app.post("/api/decisions")
async def create_decision(decision: DecisionCreate) -> dict:
    """Create a new decision from the dashboard.

    Used by BA and non-technical team members.
    Calls GraphClient.add_decision() directly.
    Requires ANTHROPIC_API_KEY to be set.

    Args:
        decision: DecisionCreate payload from the form.

    Returns:
        Dict with success bool and decision id.
    """
    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not api_key:
        raise HTTPException(
            status_code=503,
            detail="ANTHROPIC_API_KEY not set",
        )
    try:
        gc = _get_graph_client()
        if gc is None:
            raise RuntimeError("Graph client unavailable")
        content = decision.rationale
        if decision.alternatives:
            content += f"\n\nAlternatives considered: {', '.join(decision.alternatives)}"
        result = await gc.add_decision(
            title=decision.title,
            content=content,
            source_type=decision.source_type,
            project="smm-sync",
            is_constraint=decision.is_constraint,
        )
        return {"success": True, "id": result.get("uuid") if isinstance(result, dict) else None}
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


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

    Returns:
        Dict with items list and grouped dict (backlog/in_progress/done).
    """
    items = await asyncio.to_thread(_load_board)
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
            for key in ("title", "description", "status", "type", "priority"):
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

def run_dashboard(host: str = "127.0.0.1", port: int = 7842) -> None:
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
                    f"Port 7842 unavailable, using {port}",
                    file=sys.stderr
                )
        finally:
            try:
                sock.close()
            except Exception as e:
                print(f"[dashboard] run_dashboard socket close error: {e}", file=sys.stderr)

    print(f"CaaS Dashboard running at http://{host}:{port}", file=sys.stderr)
    uvicorn.run(app, host=host, port=port, log_level="warning")
