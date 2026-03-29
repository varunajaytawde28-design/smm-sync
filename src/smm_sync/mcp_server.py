"""SMM-Sync MCP server — 14 tools for AI agent coordination and context retrieval.

Coordination tools (original 4):
  read_context      — get full project context + coordination state
  claim_file        — atomically claim a file
  release_file      — release a claimed file
  refresh_context   — check if AGENTS.md changed, re-parse if so

Context graph tools (original 4):
  query_decisions       — search team decisions and architectural knowledge
  add_decision          — record a new team decision in the knowledge graph
  get_project_context   — comprehensive project context from the graph
  check_constraints     — check if a proposed action violates known constraints

Sprint tools (3):
  get_decision_timeline   — chronological history of decisions on a topic
  get_compliance_lineage  — audit trail for a session or specific decision
  add_constraint          — register a non-negotiable project constraint

CaaS tools (3):
  get_path_context    — JIT path-based rule injection for a file being edited
  get_board_items     — read the decision board (.smm/board.json)
  update_board_item   — create/update/move a board item
"""
from __future__ import annotations

import atexit
import hashlib
import json
import logging
import os
import re
import shlex
import subprocess
import sys
from pathlib import Path

from mcp.server.fastmcp import FastMCP

from smm_sync.config import DASHBOARD_PORT
from smm_sync.coordinator import claim as _coord_claim
from smm_sync.coordinator import is_claimed, release as _coord_release
from smm_sync.ingester import ingest, load_parsed_context
from smm_sync.state import get_current_state, propose, read_events

logger = logging.getLogger(__name__)
mcp = FastMCP("smm-sync")
_smm_dir: Path | None = None
_graph_client = None  # Lazy-initialised GraphClient
_context_loaded: bool = False  # Set to True after get_project_context succeeds
_project_hash: str = ""  # SHA-256[:8] of project root path, set in run_server

_SESSION_NOT_INIT_ERROR = {
    "content": [{"type": "text", "text": (
        "⚠️ SESSION NOT INITIALIZED: You must call get_project_context before using "
        "any other Axiom Hub tool. This loads your project's architectural decisions "
        "and checks for unresolved contradictions."
    )}],
    "isError": True,
}


def _is_session_killed(session_id: str) -> bool:
    """Check if this session has been disconnected from the dashboard.

    Args:
        session_id: Session identifier to check.

    Returns:
        True if the session has been killed, False otherwise.
    """
    if not session_id or _smm_dir is None:
        return False
    killed_path = _smm_dir / "killed_sessions.json"
    try:
        killed = json.loads(killed_path.read_text(encoding="utf-8"))
        return session_id in killed.get("sessions", [])
    except Exception:
        return False


_KILLED_MESSAGE = (
    "⚡ This agent session has been disconnected by the dashboard. "
    "Start a new Claude Code session to reconnect."
)


def _get_smm_dir() -> Path:
    """Return the configured .smm directory.

    Returns:
        Path to .smm directory.

    Raises:
        RuntimeError: If run_server() has not been called yet.
    """
    if _smm_dir is None:
        raise RuntimeError("MCP server not initialised. Call run_server(smm_dir) first.")
    return _smm_dir


def _get_agents_md() -> Path:
    """Return path to AGENTS.md (parent of .smm/).

    Returns:
        Path to AGENTS.md file.
    """
    return _get_smm_dir().parent / "AGENTS.md"


# ---------------------------------------------------------------------------
# Architectural violation detection
# ---------------------------------------------------------------------------

# Maps superseded-decision keywords to grep patterns and file extensions.
_KEYWORD_PATTERNS: dict[str, dict] = {
    "sqlite": {"grep": ["sqlite", "sqlite3"], "find": ["*.db", "*.sqlite"]},
    "postgresql": {"grep": ["psycopg2", "postgresql"], "find": []},
    "celery": {"grep": ["celery", "Celery"], "find": []},
    "backgroundtasks": {"grep": ["BackgroundTasks", "background_tasks"], "find": []},
    "api_key": {"grep": ["X-API-Key", "api_key", "drone_secret"], "find": []},
    "jwt": {"grep": ["python-jose", "jwt"], "find": []},
}

_VERIFICATION_PROTOCOL_MSG = (
    "⚠ VERIFICATION PROTOCOL: You are connected to a REAL terminal.\n"
    "THOUGHT ≠ ACTION: Writing 'I checked' in your reasoning does nothing.\n"
    "You MUST execute the actual bash commands to verify compliance.\n"
    "Do NOT state 'All items satisfied' without running the verification "
    "commands and pasting their output."
)


def _extract_keywords(title: str) -> list[str]:
    """Extract recognised technical keywords from a decision title.

    Args:
        title: Decision title to scan (e.g. "SQLite via SQLAlchemy ORM").

    Returns:
        List of lowercase keyword strings that appear in _KEYWORD_PATTERNS.
    """
    words = re.split(r"[\s\-_/]+", title.lower())
    return [w for w in words if w in _KEYWORD_PATTERNS]


def _run_verification(smm_dir: Path) -> list[str]:
    """Scan the codebase for architectural violations from resolved contradictions.

    Reads .smm/contradiction_index.json, finds resolved pairs, extracts
    keywords from SUPERSEDED decision titles, then runs grep/find against
    the project root.  Each subprocess is limited to 5 seconds.

    Args:
        smm_dir: Path to the .smm directory.

    Returns:
        List of human-readable violation strings.  Empty means clean.
    """
    idx_path = smm_dir / "contradiction_index.json"
    if not idx_path.exists():
        return []

    try:
        idx = json.loads(idx_path.read_text(encoding="utf-8"))
    except Exception:
        return []

    resolved_pairs = [p for p in idx.get("pairs", []) if p.get("status") == "resolved"]
    if not resolved_pairs:
        return []

    project_root = str(smm_dir.parent)
    violations: list[str] = []

    for pair in resolved_pairs:
        loser = pair.get("decision_b_title", "")   # superseded decision
        winner = pair.get("decision_a_title", "")  # kept decision
        if not loser:
            continue

        keywords = _extract_keywords(loser)
        if not keywords:
            continue

        for kw in keywords:
            patterns = _KEYWORD_PATTERNS[kw]

            for grep_pat in patterns.get("grep", []):
                try:
                    result = subprocess.run(
                        ["grep", "-rn", grep_pat, "--include=*.py", project_root],
                        capture_output=True,
                        text=True,
                        timeout=5,
                    )
                    if result.returncode == 0 and result.stdout.strip():
                        for line in result.stdout.strip().splitlines():
                            violations.append(
                                f"Code uses '{grep_pat}' (superseded by '{winner}'): "
                                f"{line.strip()}"
                            )
                except (subprocess.TimeoutExpired, OSError) as e:
                    logger.warning(f"Failed to search for pattern '{grep_pat}': {e}")

            for file_pat in patterns.get("find", []):
                try:
                    result = subprocess.run(
                        ["find", project_root, "-name", file_pat],
                        capture_output=True,
                        text=True,
                        timeout=5,
                    )
                    if result.returncode == 0 and result.stdout.strip():
                        for fpath in result.stdout.strip().splitlines():
                            violations.append(
                                f"File '{fpath}' exists — DELETE it "
                                f"(superseded by '{winner}')"
                            )
                except (subprocess.TimeoutExpired, OSError) as e:
                    logger.warning(f"Failed to find files matching '{file_pat}': {e}")

    return violations


def _build_block_message(violations: list[str]) -> str:
    """Format a BLOCKED error message listing violations and fix commands.

    Args:
        violations: List of violation strings from _run_verification().

    Returns:
        Human-readable blocking message for the agent.
    """
    numbered = "\n".join(f"{i}. {v}" for i, v in enumerate(violations, 1))

    # Derive fix commands: rm for file violations, grep reminder for code violations
    fix_cmds: list[str] = []
    for v in violations:
        if v.startswith("File '") and "DELETE" in v:
            # Extract the file path between quotes
            m = re.search(r"File '([^']+)'", v)
            if m:
                fix_cmds.append(f"rm {shlex.quote(m.group(1))}")
        elif "Code uses '" in v:
            m = re.search(r"Code uses '([^']+)'", v)
            if m:
                cmd = (
                    f"grep -rn {shlex.quote(m.group(1))} --include='*.py' ."
                )
                if cmd not in fix_cmds:
                    fix_cmds.append(cmd)

    fix_section = "\n".join(fix_cmds) if fix_cmds else "Review violations above and remove superseded code."

    return (
        "BLOCKED: Architectural violations detected. You must fix these "
        "BEFORE accessing project context.\n\n"
        f"VIOLATIONS:\n{numbered}\n\n"
        f"COMMANDS TO FIX:\n{fix_section}\n\n"
        "After fixing, call get_project_context again."
    )


@mcp.tool(structured_output=False)
def read_context() -> str:
    """Return current AGENTS.md content plus active coordination state.

    Agents call this at session start to get full project context:
    architectural decisions, active task, claimed files, active sessions.

    Returns:
        Formatted string combining AGENTS.md content with live state.
    """
    global _context_loaded
    if not _context_loaded:
        return _SESSION_NOT_INIT_ERROR
    smm_dir = _get_smm_dir()
    agents_md = _get_agents_md()

    agents_content = ""
    if agents_md.exists():
        agents_content = agents_md.read_text(encoding="utf-8")
    else:
        agents_content = "(No AGENTS.md found. Run `smm init` first.)"

    state = get_current_state(smm_dir)
    claimed = state.get("claimed_files", {})
    sessions = state.get("active_sessions", {})

    state_lines = ["\n---\n## Coordination State\n"]
    if claimed:
        state_lines.append("### Claimed Files")
        for fp, info in claimed.items():
            task = info.get("task", "")
            task_str = f" — {task}" if task else ""
            state_lines.append(f"- `{fp}` claimed by `{info['session_id']}`{task_str}")
    else:
        state_lines.append("No files currently claimed.")

    if sessions:
        state_lines.append("\n### Active Sessions")
        for sid, info in sessions.items():
            files = info.get("files", [])
            state_lines.append(f"- `{sid}`: {', '.join(files) if files else 'no files claimed'}")

    last_refresh = state.get("last_refresh", "")
    if last_refresh:
        state_lines.append(f"\nLast context refresh: {last_refresh}")

    return agents_content + "\n".join(state_lines)


@mcp.tool(structured_output=False)
def claim_file(filepath: str, session_id: str, task: str = "") -> dict:
    """Atomically claim a file for exclusive editing.

    Uses propose-validate-commit. Agents must call this before editing
    any file to avoid conflicts with other simultaneous sessions.

    Args:
        filepath: Relative path of file to claim.
        session_id: Unique identifier for this agent session.
        task: Optional description of what this agent is doing.

    Returns:
        Dict with keys:
            success (bool): True if claim succeeded.
            conflict (str): Description of conflict if success=False.
    """
    global _context_loaded
    if not _context_loaded:
        return _SESSION_NOT_INIT_ERROR
    smm_dir = _get_smm_dir()

    if not _coord_claim(smm_dir, filepath, session_id):
        state = get_current_state(smm_dir)
        owner = state.get("claimed_files", {}).get(filepath, {}).get("session_id", "unknown")
        return {"success": False, "conflict": f"{filepath} is already claimed by {owner}"}

    result = propose(smm_dir, "file_claimed", session_id, {"filepath": filepath, "task": task})
    if not result["accepted"]:
        _coord_release(smm_dir, filepath)
        return {"success": False, "conflict": result["reason"]}

    return {"success": True}


@mcp.tool(structured_output=False)
def release_file(filepath: str, session_id: str) -> dict:
    """Release a claimed file.

    Agents call this after completing edits to a file.

    Args:
        filepath: Relative path of file to release.
        session_id: Identifier of the session that claimed the file.

    Returns:
        Dict with keys:
            success (bool): True if release succeeded.
            reason (str): Failure reason if success=False.
    """
    global _context_loaded
    if not _context_loaded:
        return _SESSION_NOT_INIT_ERROR
    smm_dir = _get_smm_dir()

    result = propose(smm_dir, "file_released", session_id, {"filepath": filepath})
    if not result["accepted"]:
        return {"success": False, "reason": result["reason"]}

    _coord_release(smm_dir, filepath)
    return {"success": True}


@mcp.tool(structured_output=False)
def refresh_context(session_id: str) -> dict:
    """Check if AGENTS.md changed; re-parse if so.

    Agents call this after a git commit lands to pick up context updates.

    Args:
        session_id: Identifier of the calling session.

    Returns:
        Dict with keys:
            changed (bool): True if AGENTS.md was updated.
            context (str): New parsed context summary if changed=True.
            reason (str): Reason for no change if changed=False.
    """
    global _context_loaded
    if not _context_loaded:
        return _SESSION_NOT_INIT_ERROR
    smm_dir = _get_smm_dir()
    agents_md = _get_agents_md()

    if not agents_md.exists():
        return {"changed": False, "reason": "AGENTS.md not found"}

    content = agents_md.read_text(encoding="utf-8")
    new_hash = hashlib.sha256(content.encode()).hexdigest()

    result = propose(smm_dir, "context_refreshed", session_id, {
        "context_hash": new_hash,
        "agents_md_path": str(agents_md),
    })

    if not result["accepted"]:
        return {"changed": False, "reason": result["reason"]}

    parsed = ingest(smm_dir, agents_md)
    return {
        "changed": True,
        "context": parsed.get("project", "") + "\n\n" + parsed.get("active_task", ""),
    }


async def _check_github_auth() -> bool:
    """Check if GitHub authentication is still valid.

    Returns True if auth is valid, False otherwise. Never raises.
    """
    import os
    token = os.environ.get("GITHUB_TOKEN", "")
    if not token:
        return False
    try:
        from github import Auth, Github
        gh = Github(auth=Auth.Token(token))
        _ = gh.get_user().login
        return True
    except Exception:
        return False


def _get_graph_client():
    """Return (lazily initialise) the GraphClient for the context graph.

    Uses the module-level singleton from context_graph.client for performance:
    eliminates 2-3 second sentence-transformer model reload between MCP calls.

    Returns:
        GraphClient instance, or None if context_graph is unavailable.
    """
    global _graph_client
    if _graph_client is None:
        try:
            from smm_sync.context_graph.client import get_graph_client

            smm_dir = _get_smm_dir()
            graph_dir = smm_dir / "graph"
            _graph_client = get_graph_client(graph_dir=graph_dir)
        except Exception:
            return None
    return _graph_client


def _get_lineage_logger():
    """Return the compliance lineage logger for the current smm_dir.

    Returns:
        LineageLogger instance, or None if unavailable.
    """
    try:
        from smm_sync.compliance.lineage import LineageLogger

        smm_dir = _get_smm_dir()
        log_path = smm_dir / "compliance_lineage.jsonl"
        return LineageLogger(log_path)
    except Exception:
        return None


def _mark_context_loaded() -> None:
    """Mark this session as initialised and create the lock file for Claude Code hook."""
    global _context_loaded
    _context_loaded = True
    if _project_hash:
        try:
            Path(f"/tmp/smm-session-{_project_hash}.lock").touch()
        except Exception:
            pass


def _time_saved_footer() -> str:
    """Build a one-line time saved footer for MCP tool responses.

    Reads compliance_lineage.jsonl for injection count in the last 7 days.
    Formula: injections × 3.75 min.

    Returns:
        Footer string to append to tool responses.
    """
    try:
        import json as _json
        from datetime import datetime, timedelta, timezone

        smm_dir = _smm_dir
        if smm_dir is None:
            return ""
        lineage_path = smm_dir / "compliance_lineage.jsonl"
        if not lineage_path.exists():
            return ""
        cutoff = datetime.now(timezone.utc) - timedelta(days=7)
        count = 0
        with open(lineage_path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = _json.loads(line)
                    if entry.get("event_type") != "context_injection":
                        continue
                    ts_str = entry.get("timestamp", "")
                    if ts_str:
                        ts = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
                        if ts >= cutoff:
                            count += 1
                except Exception:
                    continue
        mins_week = int(count * 3.75)
        h, m = divmod(mins_week, 60)
        week_str = f"~{h}h {m}m" if h > 0 else f"~{m}m"
        return (
            f"\n\n---\n"
            f"⏱ CaaS: saved ~3.75 min · Total this week: {week_str}"
        )
    except Exception:
        return ""


@mcp.tool(structured_output=False)
async def query_decisions(
    query: str,
    project: str = "smm-sync",
    limit: int = 5,
    session_id: str = "",
) -> str:
    """Search team decisions and architectural knowledge.

    Call this when you need to understand WHY something was built a certain way,
    or what constraints exist before making changes.

    Args:
        query: Natural language question about the project.
               e.g. "why did we choose Kuzu?"
               e.g. "what are the constraints on the MCP server?"
               e.g. "what alternatives were considered for file locking?"
        project: Project name (default: smm-sync).
        limit: Max results to return (default: 5).
        session_id: Optional session identifier (used for kill check).

    Returns:
        Formatted string of relevant decisions with rationale.
    """
    global _context_loaded
    if not _context_loaded:
        return _SESSION_NOT_INIT_ERROR
    if session_id and _is_session_killed(session_id):
        return _KILLED_MESSAGE
    client = _get_graph_client()
    if client is None:
        return "Context graph unavailable. Run `smm seed-graph` to populate it."

    try:
        results = await client.search_context(query=query, project=project, limit=limit)
    except Exception as e:
        return f"Graph search failed: {e}"

    # Log to compliance lineage
    logger = _get_lineage_logger()
    if logger and results:
        logger.log_context_injection(
            query=query,
            decisions_surfaced=[r.title for r in results],
            agent="mcp-client",
            tool_name="query_decisions",
        )

    # Déjà Vu check — zero LLM calls, purely graph similarity
    rejections = await client.check_rejected_alternatives(query=query, project=project)

    if not results and not rejections:
        return f"No relevant decisions found for query: {query!r} in project {project!r}."

    lines = [f"## Decisions relevant to: {query!r}\n"]

    if rejections:
        lines.append("⚠️  DÉJÀ VU WARNING")
        lines.append("=" * 40)
        lines.append(
            "This query resembles previously-rejected alternatives. "
            "Review before proceeding:\n"
        )
        for r in rejections:
            lines.append(f"**Decision:** {r.decision_title}")
            lines.append(f"**Reason rejected:** {r.rationale[:300]}")
            lines.append(f"**Confidence:** {r.confidence:.2f}")
            lines.append("")
        lines.append("=" * 40 + "\n")

    for i, r in enumerate(results, 1):
        lines.append(f"### {i}. {r.title}")
        lines.append(r.content)
        if r.excerpt and r.excerpt != r.content:
            lines.append(f"\n*Excerpt:* {r.excerpt}")
        lines.append("")
    return "\n".join(lines) + _time_saved_footer()


@mcp.tool(structured_output=False)
async def add_decision(
    title: str,
    content: str,
    rationale: str,
    made_by: str,
    project: str = "smm-sync",
    constraints: list[str] = [],
    alternatives: list[str] = [],
    decision_type: str = "technical",
    confidence: float | None = None,
) -> dict:
    """Record an architectural, technical, product, or constraint decision.

    Call whenever choosing between two or more alternatives. Required fields:
    title, description, type, confidence.

    Args:
        title: Short title of the decision.
        content: Full description of what was decided.
        rationale: Why this decision was made.
        made_by: Who made this decision.
        project: Project name (default: smm-sync).
        constraints: Known constraints imposed by this decision.
        alternatives: Alternatives that were considered.
        decision_type: One of 'architectural', 'technical', 'product', 'constraint'.
        confidence: Confidence score (0.0–1.0).

    Returns:
        Dict with keys: success (bool), decision_id (str) or error (str).
    """
    global _context_loaded
    if not _context_loaded:
        return _SESSION_NOT_INIT_ERROR
    if confidence is not None and not (0.0 <= confidence <= 1.0):
        return {"success": False, "error": f"Confidence must be between 0.0 and 1.0, got {confidence!r}"}
    client = _get_graph_client()
    if client is None:
        return {"success": False, "error": "Context graph unavailable."}

    import os as _os
    _use_local = not _os.environ.get("ANTHROPIC_API_KEY", "").strip()
    try:
        if _use_local:
            decision_id = await client.add_decision_local(
                title=title,
                content=content,
                rationale=rationale,
                made_by=made_by,
                project=project,
                constraints=list(constraints),
                alternatives=list(alternatives),
                decision_type=decision_type,
            )
        else:
            decision_id = await client.add_decision(
                title=title,
                content=content,
                rationale=rationale,
                made_by=made_by,
                project=project,
                constraints=list(constraints),
                alternatives=list(alternatives),
                decision_type=decision_type,
            )

        # Bug 4 fix: also write to decisions.jsonl so `smm check` can see it.
        try:
            from smm_sync.jsonl_writer import write_decision as _write_decision
            _write_decision({
                "title": title,
                "rationale": rationale or content,
                "type": decision_type,
                "confidence": confidence,
                "alternatives": list(alternatives),
                "constraints": list(constraints),
                "made_by": made_by,
                "project": project,
                "source": "mcp",
            }, project=project)
        except Exception:
            pass  # never block graph write on JSONL failure

        return {"success": True, "decision_id": decision_id}
    except Exception as e:
        return {"success": False, "error": str(e)}


@mcp.tool(structured_output=False)
async def get_project_context(project: str = "smm-sync", session_id: str = "") -> str:
    """CRITICAL: Must be called FIRST before any other tool in every new session.

    Returns active architectural decisions, unresolved contradictions, and
    project configuration.

    Args:
        project: Project name (default: smm-sync).
        session_id: Optional session identifier (used for kill check).

    Returns:
        Structured summary of all project decisions and constraints.
    """
    if session_id and _is_session_killed(session_id):
        return _KILLED_MESSAGE

    import json as _json
    from datetime import datetime as _dt, timedelta, timezone as _tz
    smm_dir = _get_smm_dir()

    # ── Enforcement: check for architectural violations before returning context ─
    violations = _run_verification(smm_dir)
    if violations:
        raise RuntimeError(_build_block_message(violations))

    # Session is valid — mark as initialised so other tools become available.
    _mark_context_loaded()

    # ── Primary source: decisions.jsonl ──────────────────────────────────────
    _jsonl_path = smm_dir / "decisions.jsonl"
    _jsonl_rows: list[dict] = []
    if _jsonl_path.exists():
        for _raw in _jsonl_path.read_text(encoding="utf-8").splitlines():
            _raw = _raw.strip()
            if _raw:
                try:
                    _jsonl_rows.append(_json.loads(_raw))
                except Exception as e:
                    logger.warning(f"Failed to parse JSONL line '{_raw[:50]}...': {e}")

    # Build decision metadata from JSONL when available, else fall back to Kuzu.
    _type_counts: dict[str, int] = {}
    _decision_meta: list[tuple[str, str, float]] = []  # (title, type, confidence)
    _all_titles: list[str] = []
    _all_rationales: list[tuple[str, str]] = []  # (title, rationale)

    if _jsonl_rows:
        for _jd in _jsonl_rows:
            _dt_val = (_jd.get("type") or "technical").lower()
            _conf = float(_jd.get("confidence") or 0.80)
            _title = _jd.get("title") or ""
            _rat = _jd.get("rationale") or ""
            _type_counts[_dt_val] = _type_counts.get(_dt_val, 0) + 1
            _decision_meta.append((_title, _dt_val, _conf))
            _all_titles.append(_title)
            _all_rationales.append((_title, _rat))
        decisions_count = len(_jsonl_rows)
    else:
        # Fall back to Kuzu
        client = _get_graph_client()
        if client is None:
            return "Context graph unavailable. Run `smm seed-graph` or `smm add-decision` first."
        try:
            decisions = await client.get_decisions(project=project)
        except Exception as e:
            return f"Failed to retrieve project context: {e}"

        if not decisions:
            return (
                f"No decisions found for project {project!r}. "
                "Run `smm seed-graph` to populate the graph, or use `smm add-decision`."
            )

        for d in decisions:
            _content = (d.content or "").replace("\\n", "\n")
            _dt_val = "architectural"
            _conf = 0.80
            for _ln in _content.splitlines():
                if _ln.startswith("Decision type:"):
                    _dt_val = _ln.split(":", 1)[1].strip().lower()
                elif _ln.startswith("Confidence:"):
                    try:
                        _conf = float(_ln.split(":", 1)[1].strip())
                    except Exception as e:
                        logger.warning(f"Failed to parse confidence value from line '{_ln}': {e}")
            _type_counts[_dt_val] = _type_counts.get(_dt_val, 0) + 1
            _decision_meta.append((d.title or "", _dt_val, _conf))
            _all_titles.append(d.title or "")
            _rat = getattr(d, "rationale", "") or ""
            _all_rationales.append((d.title or "", _rat))
        decisions_count = len(decisions)

        # Log to compliance lineage (Kuzu path only)
        logger = _get_lineage_logger()
        if logger and decisions:
            logger.log_context_injection(
                query=f"get_project_context:{project}",
                decisions_surfaced=[d.title for d in decisions],
                agent="mcp-client",
                tool_name="get_project_context",
            )

    if not _decision_meta:
        return (
            f"No decisions found for project {project!r}. "
            "Run `smm seed-graph` to populate the graph, or use `smm add-decision`."
        )

    # Read active contradictions from contradictions.jsonl
    active_contras: list[dict] = []
    try:
        contra_path = smm_dir / "contradictions.jsonl"
        if contra_path.exists():
            for _line in contra_path.read_text(encoding="utf-8").splitlines():
                _line = _line.strip()
                if _line:
                    try:
                        _c = _json.loads(_line)
                        if not _c.get("resolved", False):
                            active_contras.append(_c)
                    except Exception as e:
                        logger.warning(f"Failed to parse contradiction line '{_line[:50]}...': {e}")
    except Exception as e:
        logger.warning(f"Failed to read contradictions file: {e}")

    # Header
    _type_summary = ", ".join(f"{v} {k}" for k, v in sorted(_type_counts.items()))
    lines = [
        "PROJECT CONTEXT:",
        f"{decisions_count} decisions recorded ({_type_summary}). "
        f"{len(active_contras)} active contradiction{'s' if len(active_contras) != 1 else ''}.",
        "",
    ]

    # ACTION REQUIRED — injected before decisions so agents see it first
    action_lines: list[str] = []
    resolution_lines: list[str] = []
    try:
        idx_path = smm_dir / "contradiction_index.json"
        if idx_path.exists():
            idx = _json.loads(idx_path.read_text(encoding="utf-8"))
            cutoff_7 = _dt.now(_tz.utc) - timedelta(days=7)
            cutoff_30 = _dt.now(_tz.utc) - timedelta(days=30)

            for pair in idx.get("pairs", []):
                if pair.get("status") != "resolved":
                    continue
                actioned_str = pair.get("actioned_at", "")
                if not actioned_str:
                    continue
                try:
                    actioned_dt = _dt.fromisoformat(actioned_str.replace("Z", "+00:00"))
                except Exception:
                    continue

                winner = pair.get("decision_a_title", "")
                loser = pair.get("decision_b_title", "")
                note = pair.get("note", "")
                date_str = actioned_str[:10]

                already_done = False
                # Check Kuzu for "Implemented" markers if graph client is available.
                _kuzu_client = _get_graph_client()
                if _kuzu_client is not None:
                    try:
                        impl_rows, _, _ = await _kuzu_client._driver.execute_query(
                            "MATCH (e:Episodic) WHERE e.name STARTS WITH 'Implemented PM resolution' "
                            "RETURN e.name LIMIT 50"
                        )
                        for r in impl_rows:
                            name = r.get("e.name", "") or ""
                            if winner and winner[:30].lower() in name.lower():
                                already_done = True
                                break
                    except Exception as e:
                        logger.warning(f"Failed to query graph for implementation status: {e}")

                if actioned_dt >= cutoff_7 and not already_done:
                    action_lines.append(
                        f"ACTION REQUIRED — PM Resolution ({date_str}):\n"
                        f"  KEEP: '{winner}'\n"
                        f"  SUPERSEDED: '{loser}'\n"
                        + (f"  PM note: {note}\n" if note else "")
                        + f"  Before starting new work:\n"
                        f"  1. Find code that uses '{loser}' and refactor to '{winner}'\n"
                        f"  2. Mark as done: echo '{{\"title\":\"Implemented PM resolution: {winner[:50]}\","
                        f"\"rationale\":\"Replaced {loser[:40]} on {date_str}\","
                        f"\"type\":\"architectural\",\"confidence\":0.95}}' | smm add-decision --local -\n"
                        f"  3. Commit before new feature work"
                    )
                elif actioned_dt >= cutoff_30:
                    resolution_lines.append(
                        f"  ✓ KEEP: '{winner}' — SUPERSEDED: '{loser}' ({date_str})"
                        + (f"\n    Note: {note}" if note else "")
                    )
    except Exception:
        pass  # never block context retrieval on resolution lookup failure

    if action_lines:
        lines.append(_VERIFICATION_PROTOCOL_MSG)
        lines.append("")
        lines.append("ACTION REQUIRED — Implement these PM resolutions:")
        for i, al in enumerate(action_lines, 1):
            lines.append(f"{i}. {al}")
        lines.append("")

    # Active contradictions section — always show (Design fix)
    if active_contras:
        lines.append(f"⚠ UNRESOLVED CONTRADICTIONS ({len(active_contras)}):")
        for i, ac in enumerate(active_contras, 1):
            da = ac.get("decision_a", "")
            db = ac.get("decision_b", "")
            conf = ac.get("confidence")
            conf_str = f" — Confidence: {conf:.2f}" if conf is not None else ""
            lines.append(f'  {i}. "{da}" CONTRADICTS "{db}"{conf_str}')
        lines.append("")
        lines.append("  ACTION REQUIRED: Resolve these contradictions on the dashboard (smm dashboard) before proceeding.")
        lines.append("")
    else:
        lines.append("✅ No unresolved contradictions")
        lines.append("")

    # Resolved contradictions (last 30 days)
    if resolution_lines:
        lines.append("RESOLVED CONTRADICTIONS (last 30 days):")
        lines.extend(resolution_lines)
        lines.append("")

    # Recent decisions
    lines.append("RECENT DECISIONS:")
    for i, (title, dtype, conf) in enumerate(_decision_meta[:10], 1):
        lines.append(f"{i}. {title} [{dtype}, {conf:.2f}]")
    if len(_decision_meta) > 10:
        lines.append(f"... and {len(_decision_meta) - 10} more.")
    lines.append("")

    # Full decision list
    lines.append("ALL DECISIONS:")
    for i, (title, rationale) in enumerate(_all_rationales, 1):
        lines.append(f"\n{i}. {title}")
        if rationale:
            lines.append(f"   Rationale: {rationale[:200]}")

    response = "\n".join(lines)

    # Auth check — warn if GitHub sync is broken (Fix 3)
    github_ok = await _check_github_auth()
    if not github_ok:
        response += (
            "\n\n\u26a0\ufe0f WARNING: GitHub sync is broken. "
            "Context may be stale. "
            "Check GITHUB_TOKEN environment variable."
        )

    return response + _time_saved_footer()


@mcp.tool(structured_output=False)
async def check_constraints(
    proposed_action: str,
    project: str = "smm-sync",
    session_id: str = "",
) -> dict:
    """Check if a proposed action violates any known project constraints.

    Call this before making significant architectural changes, adding dependencies,
    or modifying core systems.

    Args:
        proposed_action: What you are about to do.
                        e.g. "replace Kuzu with FalkorDB"
                        e.g. "add LWW CRDT back to state.py"
                        e.g. "expose raw MCP without gateway"
        project: Project name (default: smm-sync).
        session_id: Optional session identifier (used for kill check).

    Returns:
        Dict with keys:
            conflicts (list[str]): Decisions this action directly conflicts with.
            warnings (list[str]): Decisions worth reviewing before proceeding.
            clear (bool): True if no conflicts or warnings found.
    """
    global _context_loaded
    if not _context_loaded:
        return _SESSION_NOT_INIT_ERROR
    if session_id and _is_session_killed(session_id):
        return {"conflicts": [_KILLED_MESSAGE], "warnings": [], "clear": False}
    client = _get_graph_client()
    if client is None:
        return {
            "conflicts": [],
            "warnings": ["Context graph unavailable — cannot check constraints."],
            "clear": False,
        }

    try:
        # Search for decisions related to the proposed action
        results = await client.search_context(
            query=proposed_action,
            project=project,
            limit=10,
        )
    except Exception as e:
        return {
            "conflicts": [],
            "warnings": [f"Graph search failed: {e}"],
            "clear": False,
        }

    # Log to compliance lineage
    logger = _get_lineage_logger()
    if logger and results:
        logger.log_context_injection(
            query=proposed_action,
            decisions_surfaced=[r.title for r in results],
            agent="mcp-client",
            tool_name="check_constraints",
        )

    # Heuristic: high relevance scores indicate potential conflicts;
    # lower scores are warnings. Graphiti doesn't expose raw scores on
    # basic search so we use content-based keyword matching as a proxy.
    conflict_keywords = [
        "do not", "never", "must not", "rejected", "prohibited",
        "not expose", "not allowed", "not safe", "forbidden",
    ]
    warning_keywords = [
        "constraint", "caution", "warning", "danger", "only",
        "requires", "must", "before", "check",
    ]

    conflicts = []
    warnings = []

    for r in results:
        content_lower = r.content.lower()
        is_conflict = any(kw in content_lower for kw in conflict_keywords)
        is_warning = any(kw in content_lower for kw in warning_keywords)

        summary = r.title or r.content[:80]
        if is_conflict:
            conflicts.append(summary)
        elif is_warning:
            warnings.append(summary)

    # Auth check — warn if GitHub sync is broken (Fix 3)
    github_ok = await _check_github_auth()
    if not github_ok:
        warnings.append(
            "\u26a0\ufe0f WARNING: GitHub sync is broken. "
            "Context may be stale. "
            "Check GITHUB_TOKEN environment variable."
        )

    return {
        "conflicts": conflicts,
        "warnings": warnings,
        "clear": len(conflicts) == 0 and len(warnings) == 0,
    }


@mcp.tool(structured_output=False)
async def get_decision_timeline(
    topic: str,
    project: str = "smm-sync",
) -> str:
    """Get the chronological history of decisions related to a topic.

    Shows how team thinking evolved, including superseded decisions.

    Call this to understand WHY the codebase evolved the way it did.
    Shows the full audit trail including decisions that were later
    reversed or superseded.

    Research basis: EVOKG (MIT CSAIL 2025) — temporal graphs that track
    superseding relationships outperform static graphs by 23.3%.

    Args:
        topic: Natural language topic (e.g. "state management", "database choice").
        project: Project name (default: smm-sync).

    Returns:
        Chronological timeline of all decisions related to the topic,
        with superseded decisions marked but not hidden.
    """
    global _context_loaded
    if not _context_loaded:
        return _SESSION_NOT_INIT_ERROR
    client = _get_graph_client()
    if client is None:
        return "Context graph unavailable. Run `smm seed-graph` to populate it."

    try:
        timeline = await client.get_decision_timeline(topic=topic, project=project)
    except Exception as e:
        return f"Timeline retrieval failed: {e}"

    if not timeline:
        return f"No decisions found for topic: {topic!r} in project {project!r}."

    lines = [f"## Decision Timeline: {topic!r}\n", f"{len(timeline)} decisions found.\n"]
    for i, entry in enumerate(timeline, 1):
        status = "✅ ACTIVE" if entry.get("valid") else "⚠️  SUPERSEDED"
        ts = entry.get("created_at") or "unknown time"
        lines.append(f"### {i}. [{status}] {entry['title']}")
        lines.append(f"*{ts}*")
        lines.append(entry.get("content", ""))
        if entry.get("superseded_note"):
            lines.append(f"\n> {entry['superseded_note']}")
        lines.append("")

    return "\n".join(lines)


@mcp.tool(structured_output=False)
async def get_compliance_lineage(
    session_id: str | None = None,
    decision_title: str | None = None,
) -> str:
    """Get the compliance audit trail.

    If session_id provided: shows all context that was injected into that
    AI coding session — exactly what the AI knew during that session.

    If decision_title provided: shows all times this decision was surfaced
    to an AI agent — the full injection history.

    Required for EU AI Act compliance and SOC 2 AI governance audits.

    Args:
        session_id: Optional session identifier to filter by.
        decision_title: Optional decision title to filter by.

    Returns:
        Formatted audit trail string.
    """
    global _context_loaded
    if not _context_loaded:
        return _SESSION_NOT_INIT_ERROR
    logger = _get_lineage_logger()
    if logger is None:
        return "Compliance lineage logger unavailable."

    if session_id:
        entries = logger.get_session_lineage(session_id)
        header = f"## Compliance Lineage for Session: {session_id}"
    elif decision_title:
        entries = logger.get_decision_lineage(decision_title)
        header = f"## Compliance Lineage for Decision: {decision_title!r}"
    else:
        entries = logger.get_all_entries()
        header = "## Compliance Lineage (all entries)"

    if not entries:
        return f"{header}\n\nNo entries found."

    lines = [header, f"\n{len(entries)} entries.\n"]
    for e in entries[:50]:  # Cap at 50 for display
        ts = e.get("timestamp", "")[:19]
        tool = e.get("tool_name") or e.get("event_type", "")
        decisions = e.get("decisions_surfaced", [])
        if decisions:
            lines.append(f"- {ts} | {tool} | surfaced: {', '.join(decisions[:3])}")
        else:
            lines.append(f"- {ts} | {e.get('event_type', '')} | {e.get('decision_title', '')}")

    if len(entries) > 50:
        lines.append(f"\n... and {len(entries) - 50} more entries.")

    return "\n".join(lines)


@mcp.tool(structured_output=False)
async def add_constraint(
    constraint: str,
    scope_keywords: list[str],
    rationale: str,
    project: str = "smm-sync",
) -> dict:
    """Register a non-negotiable project constraint.

    Constraints are different from decisions — they are rules that must never
    be violated and automatically surface in ANY query related to their scope
    keywords.

    Example:
        add_constraint(
            constraint="Never expose raw MCP to enterprise customers",
            scope_keywords=["MCP", "enterprise", "security"],
            rationale="6 fatal security flaws in raw MCP protocol"
        )

    Args:
        constraint: The constraint rule in one clear sentence.
        scope_keywords: Keywords that trigger this constraint to surface.
        rationale: Why this constraint exists.
        project: Project name (default: smm-sync).

    Returns:
        Dict with keys: success (bool), constraint_id (str) or error (str).
    """
    global _context_loaded
    if not _context_loaded:
        return _SESSION_NOT_INIT_ERROR
    client = _get_graph_client()
    if client is None:
        return {"success": False, "error": "Context graph unavailable."}

    try:
        constraint_id = await client.add_decision(
            title=f"[CONSTRAINT] {constraint[:80]}",
            content=constraint,
            rationale=rationale,
            made_by="manual-constraint",
            project=project,
            constraints=[constraint],
            alternatives=[],
            decision_type="architectural",
            source_type="manual",
        )
        return {"success": True, "constraint_id": constraint_id}
    except Exception as e:
        return {"success": False, "error": str(e)}


def _read_board(smm_dir: Path) -> list[dict]:
    """Read board items from .smm/board.json (stored as {"items": [...]}).

    Args:
        smm_dir: Path to .smm directory.

    Returns:
        List of board item dicts. Returns [] if file missing or malformed.
    """
    board_path = smm_dir / "board.json"
    if not board_path.exists():
        return []
    try:
        data = json.loads(board_path.read_text(encoding="utf-8"))
        if isinstance(data, list):
            return data
        return data.get("items", [])
    except Exception:
        return []


def _write_board(smm_dir: Path, items: list[dict]) -> None:
    """Write board items to .smm/board.json atomically ({"items": [...]}).

    Args:
        smm_dir: Path to .smm directory.
        items: List of board item dicts to persist.
    """
    smm_dir.mkdir(parents=True, exist_ok=True)
    board_path = smm_dir / "board.json"
    tmp = board_path.with_suffix(".tmp")
    tmp.write_text(json.dumps({"items": items}, indent=2), encoding="utf-8")
    tmp.replace(board_path)


@mcp.tool(structured_output=False)
async def get_path_context(
    file_path: str,
    project: str = "smm-sync",
) -> str:
    """Get just-in-time context rules relevant to the file you are about to edit.

    Call this before editing a file to surface constraints and decisions that
    apply specifically to that part of the codebase.

    Args:
        file_path: Relative or absolute path to the file being edited.
                   e.g. "src/smm_sync/mcp_server.py"
        project: Project name (default: smm-sync).

    Returns:
        Formatted string of relevant rules and constraints (up to 3).
        Returns a "no specific rules" message if the graph is empty or unavailable.
    """
    global _context_loaded
    if not _context_loaded:
        return _SESSION_NOT_INIT_ERROR
    client = _get_graph_client()
    if client is None:
        return "Context graph unavailable."

    results = await client.get_path_context(file_path=file_path, project=project)
    if not results:
        return f"No specific rules found for path: {file_path!r}"

    lines = [f"## JIT Context for `{file_path}`\n"]
    for r in results:
        lines.append(f"**{r.title}**")
        lines.append(r.excerpt or r.content[:300])
        lines.append("")
    return "\n".join(lines)


@mcp.tool(structured_output=False)
async def get_board_items(
    status: str = "",
) -> str:
    """Read the decision board from .smm/board.json.

    The board tracks decisions, tasks, and blockers in a kanban-style layout
    with statuses: backlog, in_progress, done.

    Args:
        status: Optional filter — one of "backlog", "in_progress", "done".
                Empty string returns all items.

    Returns:
        Formatted markdown list of board items.
    """
    global _context_loaded
    if not _context_loaded:
        return _SESSION_NOT_INIT_ERROR
    smm_dir = _get_smm_dir()
    items = _read_board(smm_dir)
    if not items:
        return "Board is empty. Use `update_board_item` to add items."

    if status and status != "all":
        items = [i for i in items if i.get("status") == status]

    if not items:
        return f"No board items with status: {status!r}"

    lines = ["## Decision Board\n"]
    for item in items:
        st = item.get("status", "backlog")
        icon = {"backlog": "○", "in_progress": "◑", "done": "●"}.get(st, "○")
        lines.append(f"{icon} **{item.get('title', '?')}** [{st}]")
        if item.get("description"):
            lines.append(f"  {item['description'][:200]}")
        lines.append(f"  id: {item.get('id', '?')}")
        lines.append("")
    return "\n".join(lines)


@mcp.tool(structured_output=False)
async def update_board_item(
    title: str = "",
    status: str = "backlog",
    description: str = "",
    item_id: str = "",
) -> dict:
    """Create or update an item on the decision board.

    Board items track active decisions, open questions, and blockers.
    Status flow: backlog → in_progress → done.

    Args:
        title: Short title for the board item.
        status: One of "backlog", "in_progress", "done". Default: backlog.
        description: Optional longer description or acceptance criteria.
        item_id: If provided, update existing item with this id.
                 If empty, create a new item.

    Returns:
        Dict with keys: success (bool), id (str), action ("created" or "updated").
    """
    global _context_loaded
    if not _context_loaded:
        return _SESSION_NOT_INIT_ERROR
    import uuid
    from datetime import datetime, timezone

    smm_dir = _get_smm_dir()
    items = _read_board(smm_dir)

    if item_id:
        # Update existing
        for item in items:
            if item.get("id") == item_id:
                if title:
                    item["title"] = title
                item["status"] = status
                if description:
                    item["description"] = description
                item["updated_at"] = datetime.now(timezone.utc).isoformat()
                _write_board(smm_dir, items)
                return {"success": True, "id": item_id, "action": "updated"}
        return {"success": False, "error": f"Item {item_id!r} not found."}

    # Create new
    new_id = str(uuid.uuid4())[:8]
    now = datetime.now(timezone.utc).isoformat()
    items.append({
        "id": new_id,
        "title": title,
        "status": status,
        "description": description,
        "created_at": now,
        "updated_at": now,
    })
    _write_board(smm_dir, items)
    return {"success": True, "id": new_id, "action": "created"}


def _configure_mcp_stdio():
    """Redirect all stdout to stderr for MCP stdio mode.

    MCP uses stdout exclusively for JSON-RPC messages.
    Any non-JSON output on stdout corrupts the protocol.
    This must be called before mcp.run().

    After this call:
    - print() goes to stderr (visible in terminal)
    - mcp.run() has clean stdout for JSON-RPC
    - Logging goes to stderr
    """
    import logging
    logging.basicConfig(
        stream=sys.stderr,
        level=logging.WARNING,
        format='%(levelname)s: %(message)s'
    )


def run_server(smm_dir: Path) -> None:
    """Start the MCP server bound to the given .smm directory.

    Also starts the dashboard on the configured port in a background daemon thread so
    the MCP server and dashboard share one Kuzu connection (no file-lock
    contention when CLI writes go through the dashboard HTTP API).

    Runs in foreground (blocking). Prints the transport info on start.

    Args:
        smm_dir: Path to the .smm directory for this project.
    """
    global _smm_dir, _project_hash
    _smm_dir = smm_dir
    _project_hash = hashlib.sha1(os.getcwd().encode()).hexdigest()[:8]
    atexit.register(lambda: Path(f"/tmp/smm-session-{_project_hash}.lock").unlink(missing_ok=True))

    # Root-Cause-1 fix: dashboard runs in same process → shares _graph_client
    # singleton → single Kuzu writer → no "Could not set lock on file" errors.
    import threading

    def _start_dashboard() -> None:
        try:
            from smm_sync.dashboard import run_dashboard  # type: ignore[attr-defined]
            run_dashboard(host="127.0.0.1", port=DASHBOARD_PORT)
        except Exception as exc:  # noqa: BLE001
            import sys as _sys
            print(f"[smm-sync] dashboard failed to start: {exc}", file=_sys.stderr)

    threading.Thread(target=_start_dashboard, name="smm-dashboard", daemon=True).start()

    _configure_mcp_stdio()  # MUST be before mcp.run()
    mcp.run()
