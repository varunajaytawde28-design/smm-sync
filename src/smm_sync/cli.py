"""SMM-Sync CLI — smm init, refresh, status, claim, release, serve."""
from __future__ import annotations

import os
import sys
from pathlib import Path

import click

from smm_sync.config import find_project_root, get_smm_dir
from smm_sync.coordinator import claim as _claim
from smm_sync.coordinator import is_claimed, list_claimed
from smm_sync.coordinator import release as _release
from smm_sync.git_utils import find_git_root, get_git_remote, install_pre_commit_hook
from smm_sync.ingester import ingest, load_parsed_context, migrate_smm_toml
from smm_sync.state import get_current_state, propose

_AGENTS_MD_TEMPLATE = """\
# AGENTS.md — {name}

> Source of truth for this project. Read before writing any code.
> Edit this file directly. Run `smm refresh` after editing.

## Project

{name}: Describe what this project does and why it exists.

## Architecture

### Example decision
**Why:** Reason it was made.

## Constraints

- Example: Python 3.11+ only

## Danger Zones

- Example: Do not modify the database schema without running a migration

## Modules

- `cli.py`: Entry point, Click commands

## Conventions

- All public functions must have docstrings

## Active Task

**What you are building right now.**

Describe the current sprint or feature.
"""

_MCP_CONFIG_HINT = """\

Add to your project's MCP config (e.g. .mcp.json or CLAUDE.md):

  {
    "mcpServers": {
      "smm-sync": {
        "command": "smm",
        "args": ["serve"]
      }
    }
  }
"""


@click.group()
def main() -> None:
    """SMM-Sync — shared context for simultaneous AI agents."""


@main.command()
@click.pass_context
@click.option("--name", default="", help="Project name (defaults to current directory name).")
@click.option(
    "--mode",
    default="dev",
    type=click.Choice(["dev", "dashboard"], case_sensitive=False),
    help="Install mode: 'dev' (MCP only, default) or 'dashboard' (launches web UI).",
)
def init(ctx: click.Context, name: str, mode: str) -> None:
    """Scaffold AGENTS.md and .smm/ directory, install pre-commit hook.

    Use --mode dashboard to also launch the read-only web UI at http://localhost:7842.
    Both modes share the same graph directory so PMs see what devs capture.
    """
    cwd = Path.cwd()
    project_name = name or cwd.name

    # Migrate smm.toml if present
    smm_toml = cwd / "smm.toml"
    agents_md = cwd / "AGENTS.md"
    if smm_toml.exists():
        migrate_smm_toml(smm_toml, agents_md)

    if not agents_md.exists():
        agents_md.write_text(
            _AGENTS_MD_TEMPLATE.format(name=project_name),
            encoding="utf-8",
        )
        click.echo(click.style(f"Created AGENTS.md for '{project_name}'", fg="green"))
    else:
        click.echo(click.style("AGENTS.md already exists.", fg="yellow"))

    smm_dir = cwd / ".smm"
    smm_dir.mkdir(exist_ok=True)
    (smm_dir / "locks").mkdir(exist_ok=True)
    (smm_dir / "history").mkdir(exist_ok=True)

    git_root = find_git_root(cwd)
    if git_root:
        if install_pre_commit_hook(git_root):
            click.echo(click.style("Installed pre-commit hook.", fg="green"))
        else:
            click.echo(click.style("Could not install pre-commit hook (no .git/hooks).", fg="yellow"))
    else:
        click.echo(click.style("Not in a git repo — skipping hook install.", fg="yellow"))

    # Install Axiom Lore-Hook (dev mode only)
    if mode != "dashboard":
        try:
            from smm_sync.lore_hook import (
                configure_claude_code_hook,
                configure_cursor_hook,
                configure_git_trailers,
                install_capture_script,
                install_git_hooks,
            )
            install_capture_script()
            if git_root:
                install_git_hooks(git_root)
                configure_git_trailers(git_root)

            # Ask which agent the user uses
            agent_choice = click.prompt(
                "Which AI agent do you use?",
                type=click.Choice(["claude-code", "cursor", "both", "skip"], case_sensitive=False),
                default="claude-code",
            )
            if agent_choice in ("claude-code", "both"):
                if configure_claude_code_hook():
                    click.echo(click.style("  ✓ Claude Code PreToolUse hook configured.", fg="green"))
                else:
                    click.echo(click.style("  ⚠ Could not update ~/.claude/settings.json.", fg="yellow"))
            if agent_choice in ("cursor", "both"):
                if configure_cursor_hook(cwd):
                    click.echo(click.style("  ✓ Cursor hook written to .cursor/hooks.json.", fg="green"))
            click.echo(click.style("Axiom Lore-Hook installed.", fg="green"))
        except Exception as _lh_exc:
            click.echo(
                click.style(f"Lore-Hook install warning: {_lh_exc}", fg="yellow"),
                err=True,
            )

    if mode == "dashboard":
        click.echo(click.style("\nDashboard mode — launching web UI...", fg="cyan"))
        ctx.invoke(dashboard)
    else:
        click.echo(_MCP_CONFIG_HINT)
        click.echo(click.style(
            "MCP server ready. Start coding — decisions are captured automatically.",
            fg="green",
        ))


@main.command()
@click.option("--quiet", is_flag=True, help="Suppress output.")
def refresh(quiet: bool) -> None:
    """Read AGENTS.md and update .smm/parsed_context.json."""
    project_root = find_project_root()
    agents_md = project_root / "AGENTS.md"
    smm_dir = project_root / ".smm"

    if not agents_md.exists():
        click.echo(
            click.style("No AGENTS.md found. Run `smm init` first.", fg="red"), err=True
        )
        sys.exit(1)

    smm_dir.mkdir(exist_ok=True)

    import hashlib
    content = agents_md.read_text(encoding="utf-8")
    new_hash = hashlib.md5(content.encode()).hexdigest()

    session_id = f"{os.uname().nodename}:{os.getpid()}"
    result = propose(smm_dir, "context_refreshed", session_id, {
        "context_hash": new_hash,
        "agents_md_path": str(agents_md),
    })

    if not result["accepted"] and "not changed" in result.get("reason", ""):
        if not quiet:
            click.echo("AGENTS.md unchanged — nothing to refresh.")
        return

    ingest(smm_dir, agents_md)

    if not quiet:
        click.echo(click.style("Refreshed parsed_context.json from AGENTS.md", fg="green"))


@main.command()
def status() -> None:
    """Show current coordination state: claimed files and active sessions."""
    smm_dir = get_smm_dir()

    if not smm_dir.exists():
        click.echo(click.style("No .smm/ directory found. Run `smm init` first.", fg="red"))
        sys.exit(1)

    state = get_current_state(smm_dir)

    claimed = state.get("claimed_files", {})
    if claimed:
        click.echo(click.style("Claimed files:", bold=True))
        for fp, info in claimed.items():
            click.echo(f"  {fp}  (session: {info['session_id']}, since: {info['since']})")
    else:
        click.echo("No files currently claimed.")

    sessions = state.get("active_sessions", {})
    if sessions:
        click.echo(click.style("\nActive sessions:", bold=True))
        for sid, info in sessions.items():
            files = info.get("files", [])
            files_str = ", ".join(files) if files else "none"
            click.echo(f"  {sid}  (started: {info['started']}, files: {files_str})")

    last_refresh = state.get("last_refresh", "")
    if last_refresh:
        click.echo(f"\nLast context refresh: {last_refresh}")

    click.echo(f"\n.smm/ → {smm_dir}")


@main.command()
@click.argument("filepath")
@click.option("--session", default="", help="Session identifier (defaults to hostname:pid).")
@click.option("--task", default="", help="Description of what you're doing with this file.")
def claim(filepath: str, session: str, task: str) -> None:
    """Atomically claim a file using Tuple Space + event log."""
    smm_dir = get_smm_dir()

    if not smm_dir.exists():
        click.echo(click.style("No .smm/ directory found. Run `smm init` first.", fg="red"))
        sys.exit(1)

    session_id = session or f"{os.uname().nodename}:{os.getpid()}"

    # Physical lock via coordinator (os.rename atomicity)
    if not _claim(smm_dir, filepath, session_id):
        # Physical lock taken — get details from state
        state = get_current_state(smm_dir)
        owner = state.get("claimed_files", {}).get(filepath, {}).get("session_id", "unknown")
        click.echo(click.style(f"FAILED: {filepath} is already claimed by {owner}.", fg="red"))
        sys.exit(1)

    # Record in event log
    propose(smm_dir, "file_claimed", session_id, {"filepath": filepath, "task": task})
    click.echo(click.style(f"Claimed {filepath}", fg="green"))
    click.echo(f"  session: {session_id}")


@main.command()
@click.argument("filepath")
@click.option("--session", default="", help="Session identifier.")
def release(filepath: str, session: str) -> None:
    """Release a claimed file."""
    smm_dir = get_smm_dir()

    if not smm_dir.exists():
        click.echo(click.style("No .smm/ directory found. Run `smm init` first.", fg="red"))
        sys.exit(1)

    session_id = session or f"{os.uname().nodename}:{os.getpid()}"

    if not is_claimed(smm_dir, filepath):
        click.echo(click.style(f"{filepath} is not currently claimed.", fg="yellow"))
        return

    result = propose(smm_dir, "file_released", session_id, {"filepath": filepath})
    if not result["accepted"]:
        click.echo(click.style(f"FAILED: {result['reason']}", fg="red"))
        sys.exit(1)

    _release(smm_dir, filepath)
    click.echo(click.style(f"Released {filepath}", fg="green"))


@main.command("seed-graph")
@click.option("--project", default="smm-sync", help="Project name for graph partition.")
def seed_graph(project: str) -> None:
    """Seed the context graph with 18 interconnected Axiom Hub decisions.

    Each episode body cross-references other decisions by name so Graphiti
    creates Entity nodes AND edges (SUPERSEDES, REQUIRES, ENABLES, etc.)
    — not isolated circles.

    WARNING: Makes ~54-126 Anthropic API calls and takes 5-10 minutes.
    Requires ANTHROPIC_API_KEY to be set.

    Run ONCE — the graph persists between sessions at .smm/graph/.
    """
    import asyncio

    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not api_key:
        click.echo(
            click.style(
                "ANTHROPIC_API_KEY is not set.\n"
                "Run: export ANTHROPIC_API_KEY=sk-ant-...\n"
                "Get your key from https://console.anthropic.com/settings/keys",
                fg="red",
            )
        )
        sys.exit(1)

    try:
        from smm_sync.context_graph.client import GraphClient
        from smm_sync.context_graph.seed import seed_test_data
    except ImportError as e:
        click.echo(click.style(f"context_graph module unavailable: {e}", fg="red"))
        sys.exit(1)

    smm_dir = get_smm_dir()
    if not smm_dir.exists():
        click.echo(click.style("No .smm/ directory found. Run `smm init` first.", fg="red"))
        sys.exit(1)

    graph_dir = smm_dir / "graph"
    client = GraphClient(graph_dir=graph_dir, api_key=api_key)

    click.echo(
        click.style(
            f"Starting graph seed for project '{project}'...\n"
            "18 interconnected decisions with cross-references for Entity edges.\n"
            "This will make ~54-126 API calls and take 5-10 minutes.",
            fg="yellow",
        )
    )

    asyncio.run(seed_test_data(client, project=project))
    click.echo(click.style("Graph seeded successfully.", fg="green"))


@main.command("query")
@click.argument("question")
@click.option("--project", default="smm-sync", help="Project name.")
@click.option("--limit", default=5, help="Max results.")
def query_graph(question: str, project: str, limit: int) -> None:
    """Query the context graph with a natural language question.

    Example: smm query "why did we reject LWW CRDT?"
    """
    import asyncio
    import json as _json

    try:
        from smm_sync.context_graph.client import GraphClient
    except ImportError as e:
        click.echo(click.style(f"context_graph module unavailable: {e}", fg="red"))
        sys.exit(1)

    smm_dir = get_smm_dir()
    if not smm_dir.exists():
        click.echo(click.style("No .smm/ directory found. Run `smm init` first.", fg="red"))
        sys.exit(1)

    graph_dir = smm_dir / "graph"
    client = GraphClient(graph_dir=graph_dir)

    async def _run():
        results = await client.search_context(query=question, project=project, limit=limit)
        if not results:
            click.echo(f"No results found for: {question!r}")
            return
        click.echo(click.style(f"\nResults for: {question!r}\n", bold=True))
        for i, r in enumerate(results, 1):
            click.echo(click.style(f"{i}. {r.title}", fg="green"))
            click.echo(r.content)
            click.echo()

        # Time saved footer
        saved_week = _time_saved_week(smm_dir)
        h, m = divmod(saved_week, 60)
        week_str = f"~{h}h {m}m" if h > 0 else f"~{m}m"
        click.echo("─" * 35)
        click.echo("⏱  This query saved ~3.75 min of context re-explanation.")
        click.echo(f"   Total saved this week: {week_str}")

    asyncio.run(_run())


def _time_saved_week(smm_dir: Path) -> int:
    """Calculate estimated minutes saved this week from injection log.

    Args:
        smm_dir: Path to .smm/ directory.

    Returns:
        Estimated minutes saved (injections × 3.75).
    """
    from datetime import datetime, timezone, timedelta
    import json as _json

    lineage_path = smm_dir / "compliance_lineage.jsonl"
    if not lineage_path.exists():
        return 0
    cutoff = datetime.now(timezone.utc) - timedelta(days=7)
    count = 0
    try:
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
    except Exception:
        pass
    return int(count * 3.75)


@main.command("decisions")
@click.option("--project", default="smm-sync", help="Project name.")
def list_decisions(project: str) -> None:
    """List all recorded decisions for a project."""
    import asyncio

    try:
        from smm_sync.context_graph.client import GraphClient
    except ImportError as e:
        click.echo(click.style(f"context_graph module unavailable: {e}", fg="red"))
        sys.exit(1)

    smm_dir = get_smm_dir()
    if not smm_dir.exists():
        click.echo(click.style("No .smm/ directory found. Run `smm init` first.", fg="red"))
        sys.exit(1)

    graph_dir = smm_dir / "graph"
    client = GraphClient(graph_dir=graph_dir)

    async def _run():
        decisions = await client.get_decisions(project=project)
        if not decisions:
            click.echo(f"No decisions found for project: {project!r}")
            return
        click.echo(click.style(f"\nDecisions for project: {project}\n", bold=True))
        for i, d in enumerate(decisions, 1):
            status = click.style("ACTIVE", fg="green") if d.valid else click.style("SUPERSEDED", fg="yellow")
            click.echo(f"{i:2d}. [{status}] {d.title}")

    asyncio.run(_run())


@main.command("add-decision")
@click.argument("source", type=click.File("r"), default="-", metavar="[JSON_FILE|-]")
@click.option("--project", default="smm-sync", help="Project name.")
@click.option(
    "--local",
    "use_local",
    is_flag=True,
    default=False,
    help=(
        "Write directly to Kuzu, skipping Graphiti entity extraction. "
        "Zero API calls. Uses pre-extracted JSON fields from the hook. "
        "ANTHROPIC_API_KEY not required."
    ),
)
def add_decision_cmd(source: click.File, project: str, use_local: bool) -> None:
    """Ingest a decision from JSON. Reads from stdin (-) or a file.

    JSON fields: title, content, rationale, made_by, decision_type,
    alternatives (list), constraints (list), status, confidence.

    Used by the Axiom Lore-Hook (pre-commit-capture.sh) for automatic
    capture. Can also be called directly for manual entry.

    Use --local when the hook has already extracted the structured fields
    (title, rationale, alternatives, constraints) and you want to skip
    Graphiti's Sonnet entity extraction. Zero API calls, no key needed.

    Example:
        echo '{"title":"Use Kuzu","rationale":"No Docker needed"}' | smm add-decision --local -
        echo '{"title":"Use Kuzu","rationale":"No Docker needed"}' | smm add-decision -
    """
    import asyncio
    import json as _json

    try:
        data = _json.load(source)
    except _json.JSONDecodeError as exc:
        click.echo(click.style(f"Invalid JSON: {exc}", fg="red"), err=True)
        sys.exit(1)

    try:
        from smm_sync.context_graph.client import GraphClient
    except ImportError as exc:
        click.echo(click.style(f"context_graph unavailable: {exc}", fg="red"), err=True)
        sys.exit(1)

    # Build episode body with optional Status field
    status = data.get("status", "approved")
    content = data.get("content") or data.get("rationale", "")
    # Append status line — parsed back by dashboard /api/decisions?status=
    if status != "approved":
        content = f"{content}\nStatus: {status}"

    smm_dir = get_smm_dir()
    graph_dir = smm_dir / "graph"

    if use_local:
        # Zero-API path: write pre-extracted data directly to Kuzu.
        # No ANTHROPIC_API_KEY required. Bypasses Graphiti.add_episode().
        client = GraphClient(graph_dir=graph_dir, api_key="")

        async def _run():
            return await client.add_decision_local(
                title=data.get("title", "Unnamed decision"),
                content=content,
                rationale=data.get("rationale", ""),
                made_by=data.get("made_by", "lore-hook"),
                project=project,
                alternatives=data.get("alternatives", []),
                constraints=data.get("constraints", []),
                decision_type=data.get("decision_type", "technical"),
            )
    else:
        # Full Graphiti path: entity extraction via Sonnet. Requires API key.
        api_key = os.environ.get("ANTHROPIC_API_KEY", "")
        if not api_key:
            click.echo(
                click.style(
                    "ANTHROPIC_API_KEY not set — cannot ingest decision. "
                    "Use --local to write without API calls.",
                    fg="red",
                ),
                err=True,
            )
            sys.exit(1)

        client = GraphClient(graph_dir=graph_dir, api_key=api_key)

        async def _run():
            return await client.add_decision(
                title=data.get("title", "Unnamed decision"),
                content=content,
                rationale=data.get("rationale", ""),
                made_by=data.get("made_by", "lore-hook"),
                project=project,
                alternatives=data.get("alternatives", []),
                constraints=data.get("constraints", []),
                decision_type=data.get("decision_type", "technical"),
            )

    try:
        decision_id = asyncio.run(_run())
        click.echo(click.style(f"Decision ingested: {decision_id}", fg="green"))
    except Exception as exc:
        click.echo(click.style(f"Ingestion failed: {exc}", fg="red"), err=True)
        sys.exit(1)


@main.command("check-contradictions")
@click.option("--title", required=True, help="Decision title to check.")
@click.option("--content", default="", help="Decision content/rationale.")
@click.option("--project", default="smm-sync", help="Project name.")
@click.option("--json-output", "json_output", is_flag=True, default=False,
              help="Output results as JSON (for scripting).")
def check_contradictions_cmd(title: str, content: str, project: str, json_output: bool) -> None:
    """Check if a decision contradicts existing ones in the graph.

    Used by the Axiom Lore-Hook to surface contradictions before commit.
    Returns JSON with --json-output flag for scripting.

    Example:
        smm check-contradictions --title "Use Redis" --json-output
    """
    import asyncio
    import json as _json

    try:
        from smm_sync.context_graph.client import GraphClient
    except ImportError as exc:
        if json_output:
            click.echo(_json.dumps({"contradictions": [], "error": str(exc)}))
        else:
            click.echo(click.style(f"context_graph unavailable: {exc}", fg="red"), err=True)
        return

    smm_dir = get_smm_dir()
    if not smm_dir.exists():
        result = {"contradictions": []}
        if json_output:
            click.echo(_json.dumps(result))
        return

    graph_dir = smm_dir / "graph"
    client = GraphClient(graph_dir=graph_dir, api_key=os.environ.get("ANTHROPIC_API_KEY", ""))

    async def _run():
        query = f"{title}: {content}" if content else title
        return await client.contradiction_check(query, project)

    try:
        contradictions = asyncio.run(_run())
        result = {"contradictions": contradictions}
        if json_output:
            click.echo(_json.dumps(result))
        else:
            if contradictions:
                click.echo(click.style(f"Found {len(contradictions)} contradiction(s):", fg="yellow"))
                for c in contradictions:
                    click.echo(f"  • {c['existing']} (similarity={c['similarity']:.2f})")
            else:
                click.echo(click.style("No contradictions found.", fg="green"))
    except Exception as exc:
        if json_output:
            click.echo(_json.dumps({"contradictions": [], "error": str(exc)}))
        else:
            click.echo(click.style(f"Check failed: {exc}", fg="red"), err=True)


@main.command("sync-from-git")
@click.option("--project", default="smm-sync", help="Project name.")
@click.option("--dry-run", is_flag=True, help="Show decisions without ingesting.")
def sync_from_git(project: str, dry_run: bool) -> None:
    """Parse git log Axiom-* trailers and ingest decisions into the graph.

    This is the 'new team member' path: after cloning a repo with
    Axiom trailers in its commit history, run this to populate the
    local graph without running through the live capture pipeline.

    Reads: Axiom-Decision, Axiom-Rationale, Axiom-Type, Axiom-Status
    trailers from git log and calls add_decision for each new one.

    Example:
        smm sync-from-git --dry-run
        smm sync-from-git --project my-project
    """
    import asyncio
    import json as _json
    import subprocess as _sub

    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not api_key and not dry_run:
        click.echo(
            click.style("ANTHROPIC_API_KEY not set. Use --dry-run to preview.", fg="yellow"),
            err=True,
        )
        sys.exit(1)

    # git log format: hash|date|decision|rationale|type|status|confidence
    fmt = (
        "%H|%cd|"
        "%(trailers:key=Axiom-Decision,valueonly,separator=%20)|"
        "%(trailers:key=Axiom-Rationale,valueonly,separator=%20)|"
        "%(trailers:key=Axiom-Type,valueonly,separator=%20)|"
        "%(trailers:key=Axiom-Status,valueonly,separator=%20)|"
        "%(trailers:key=Axiom-Confidence,valueonly,separator=%20)"
    )
    try:
        result = _sub.run(
            ["git", "log", "--grep=Axiom-Decision", f"--pretty=format:{fmt}", "--date=iso-strict"],
            capture_output=True,
            text=True,
            timeout=30,
        )
    except (FileNotFoundError, _sub.TimeoutExpired) as exc:
        click.echo(click.style(f"git log failed: {exc}", fg="red"), err=True)
        sys.exit(1)

    if result.returncode != 0 or not result.stdout.strip():
        click.echo("No Axiom-Decision trailers found in git log.")
        return

    decisions = []
    for line in result.stdout.splitlines():
        parts = line.split("|", 6)
        if len(parts) < 7:
            continue
        commit_hash, date, title, rationale, dtype, status, confidence = parts
        title = title.strip()
        if not title:
            continue
        decisions.append({
            "commit": commit_hash[:8],
            "date": date.strip(),
            "title": title,
            "rationale": rationale.strip(),
            "decision_type": dtype.strip() or "technical",
            "status": status.strip() or "approved",
            "confidence_label": confidence.strip() or "medium",
        })

    if not decisions:
        click.echo("No decisions parsed from git trailers.")
        return

    click.echo(f"Found {len(decisions)} decision(s) in git history:")
    for d in decisions:
        flag = click.style("[dry-run]", fg="yellow") if dry_run else ""
        click.echo(f"  {flag} [{d['commit']}] {d['title']}")

    if dry_run:
        click.echo(click.style("\nDry run — nothing ingested.", fg="yellow"))
        return

    try:
        from smm_sync.context_graph.client import GraphClient
    except ImportError as exc:
        click.echo(click.style(f"context_graph unavailable: {exc}", fg="red"), err=True)
        sys.exit(1)

    smm_dir = get_smm_dir()
    graph_dir = smm_dir / "graph"
    client = GraphClient(graph_dir=graph_dir, api_key=api_key)

    async def _ingest_all():
        for d in decisions:
            content = d["rationale"] or d["title"]
            if d["status"] != "approved":
                content = f"{content}\nStatus: {d['status']}"
            try:
                await client.add_decision(
                    title=d["title"],
                    content=content,
                    rationale=d["rationale"],
                    made_by=f"git-history ({d['commit']})",
                    project=project,
                    decision_type=d["decision_type"],
                )
                click.echo(click.style(f"  ✓ {d['title'][:60]}", fg="green"))
            except Exception as exc:
                click.echo(click.style(f"  ✗ {d['title'][:60]}: {exc}", fg="red"), err=True)

    asyncio.run(_ingest_all())
    click.echo(click.style(f"\nSync complete: {len(decisions)} decision(s) processed.", fg="green"))


@main.group("capture")
def capture_group() -> None:
    """GitHub passive capture commands."""


@capture_group.command("init")
def capture_init() -> None:
    """Create .smm/github.yml with both repos pre-configured."""
    smm_dir = get_smm_dir()
    if not smm_dir.exists():
        click.echo(click.style("No .smm/ directory found. Run `smm init` first.", fg="red"))
        sys.exit(1)

    github_yml = smm_dir / "github.yml"
    if github_yml.exists():
        click.echo(click.style(".smm/github.yml already exists.", fg="yellow"))
        return

    _GITHUB_YML_TEMPLATE = """\
# .smm/github.yml
# GitHub capture configuration for CaaS
# Commit this file — it contains no secrets.

repos:
  - owner: {owner}
    name: {repo}
    project: {project}
    capture:
      pull_requests: true
      commits: true
      issues: true
      releases: true

settings:
  poll_interval_minutes: 30
  lookback_days: 30
  min_content_length: 50
  decision_keywords:
    - "decided"
    - "chose"
    - "rejected"
    - "because"
    - "instead of"
    - "rationale"
    - "trade-off"
    - "constraint"
    - "we will"
    - "we won't"
"""
    project_name = find_project_root().name
    content = _GITHUB_YML_TEMPLATE.format(
        owner="your-github-username",
        repo=project_name,
        project=project_name,
    )
    github_yml.write_text(content, encoding="utf-8")
    click.echo(click.style("Created .smm/github.yml", fg="green"))
    click.echo("Add your repos and run 'smm capture run --once' to start capturing")


@capture_group.command("run")
@click.option("--once", is_flag=True, help="Run once and exit (default: run forever).")
@click.option(
    "--since",
    default=None,
    help=(
        "Backfill from this date. Format: YYYY-MM-DD. "
        "Example: --since 2024-01-01 fetches all PRs, "
        "commits, and issues since Jan 2024."
    ),
)
def capture_run(once: bool, since: str) -> None:
    """Run the GitHub capture pipeline.

    Requires GITHUB_TOKEN and ANTHROPIC_API_KEY to be set.
    """
    import asyncio
    from datetime import datetime, timezone

    since_date = None
    if since:
        try:
            since_date = datetime.strptime(since, "%Y-%m-%d").replace(tzinfo=timezone.utc)
            click.echo(f"Backfilling from {since_date.date()}...")
        except ValueError:
            click.echo(click.style(f"Error: --since must be in YYYY-MM-DD format, got: {since!r}", fg="red"))
            sys.exit(1)

    _load_keys_from_env_file(get_smm_dir())

    github_token = os.environ.get("GITHUB_TOKEN")
    if not github_token:
        click.echo(click.style("Error: GITHUB_TOKEN not set.", fg="red"))
        click.echo("Get a token at: https://github.com/settings/tokens")
        click.echo("Required scopes: repo (read)")
        click.echo("Then run: export GITHUB_TOKEN=ghp_your_token_here")
        sys.exit(1)

    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        click.echo(click.style("Error: ANTHROPIC_API_KEY not set.", fg="red"))
        click.echo("Get your key from https://console.anthropic.com/settings/keys")
        click.echo("Then run: export ANTHROPIC_API_KEY=sk-ant-your_key_here")
        sys.exit(1)

    try:
        from smm_sync.capture import GitHubCapture
        from smm_sync.context_graph.client import GraphClient
    except ImportError as e:
        click.echo(click.style(f"Capture module unavailable: {e}", fg="red"))
        sys.exit(1)

    smm_dir = get_smm_dir()
    if not smm_dir.exists():
        click.echo(click.style("No .smm/ directory found. Run `smm init` first.", fg="red"))
        sys.exit(1)

    config_path = smm_dir / "github.yml"
    if not config_path.exists():
        click.echo(click.style("No .smm/github.yml found. Run `smm capture init` first.", fg="red"))
        sys.exit(1)

    state_path = smm_dir / "capture_state.json"
    graph_dir = smm_dir / "graph"
    graph_client = GraphClient(graph_dir=graph_dir, api_key=api_key)

    capture = GitHubCapture(
        config_path=config_path,
        state_path=state_path,
        graph_client=graph_client,
        github_token=github_token,
        api_key=api_key,
    )

    if once or since_date:
        asyncio.run(capture.run_once(since_date=since_date))
    else:
        asyncio.run(capture.run_forever())


@capture_group.command("status")
def capture_status() -> None:
    """Show current capture state."""
    smm_dir = get_smm_dir()
    if not smm_dir.exists():
        click.echo(click.style("No .smm/ directory found. Run `smm init` first.", fg="red"))
        sys.exit(1)

    state_path = smm_dir / "capture_state.json"
    if not state_path.exists():
        click.echo("No capture state found. Run 'smm capture run --once' first.")
        return

    try:
        from smm_sync.capture.github_capture import load_capture_state
    except ImportError as e:
        click.echo(click.style(f"Capture module unavailable: {e}", fg="red"))
        sys.exit(1)

    state = load_capture_state(state_path)
    click.echo(click.style("\nGitHub Capture Status", bold=True))
    click.echo("=" * 40)
    for repo, info in state.items():
        click.echo(click.style(f"\n{repo}", fg="cyan"))
        last_run = info.get("last_run", "never")
        click.echo(f"  Last run: {last_run}")
        if pr := info.get("last_pr_number"):
            click.echo(f"  Last PR: #{pr}")
        if sha := info.get("last_commit_sha"):
            click.echo(f"  Last commit: {sha[:8]}")
        if issue := info.get("last_issue_number"):
            click.echo(f"  Last issue: #{issue}")
        if rel := info.get("last_release_id"):
            click.echo(f"  Last release ID: {rel}")


@main.group("compliance")
def compliance_group() -> None:
    """Compliance lineage audit trail commands."""


@compliance_group.command("show")
@click.option("--session", default="", help="Filter by session ID.")
@click.option("--decision", default="", help="Filter by decision title.")
def compliance_show(session: str, decision: str) -> None:
    """Show the compliance audit trail.

    Examples:
        smm compliance show --session myhost:12345
        smm compliance show --decision "Use os.rename() for atomic locking"
        smm compliance show  (shows all entries)
    """
    smm_dir = get_smm_dir()
    if not smm_dir.exists():
        click.echo(click.style("No .smm/ directory found. Run `smm init` first.", fg="red"))
        sys.exit(1)

    try:
        from smm_sync.compliance.lineage import LineageLogger
    except ImportError as e:
        click.echo(click.style(f"Compliance module unavailable: {e}", fg="red"))
        sys.exit(1)

    log_path = smm_dir / "compliance_lineage.jsonl"
    if not log_path.exists():
        click.echo("No compliance log found. Make some MCP tool calls first.")
        return

    logger = LineageLogger(log_path)

    if session:
        entries = logger.get_session_lineage(session)
        header = f"Compliance Lineage for Session: {session}"
    elif decision:
        entries = logger.get_decision_lineage(decision)
        header = f"Compliance Lineage for Decision: {decision!r}"
    else:
        entries = logger.get_all_entries()
        header = "Compliance Lineage (all entries)"

    click.echo(click.style(f"\n{header}", bold=True))
    click.echo("=" * len(header))

    if not entries:
        click.echo("No entries found.")
        return

    for e in entries:
        ts = e.get("timestamp", "")[:19]
        event_type = e.get("event_type", "")
        if event_type == "context_injection":
            tool = e.get("tool_name") or "unknown"
            count = e.get("decision_count", 0)
            surfaced = ", ".join(e.get("decisions_surfaced", [])[:2])
            click.echo(f"  {ts}  {click.style(tool, fg='cyan')}  {count} decisions  {surfaced}")
        elif event_type == "decision_added":
            title = e.get("decision_title", "")
            conf = e.get("confidence", 0)
            click.echo(f"  {ts}  {click.style('decision_added', fg='green')}  [{conf:.2f}]  {title}")

    click.echo(f"\nTotal: {len(entries)} entries")


@compliance_group.command("stats")
def compliance_stats() -> None:
    """Show compliance lineage summary statistics."""
    smm_dir = get_smm_dir()
    if not smm_dir.exists():
        click.echo(click.style("No .smm/ directory found. Run `smm init` first.", fg="red"))
        sys.exit(1)

    try:
        from smm_sync.compliance.lineage import LineageLogger
    except ImportError as e:
        click.echo(click.style(f"Compliance module unavailable: {e}", fg="red"))
        sys.exit(1)

    log_path = smm_dir / "compliance_lineage.jsonl"
    if not log_path.exists():
        click.echo("No compliance log found. Make some MCP tool calls first.")
        return

    logger = LineageLogger(log_path)
    stats = logger.get_stats()

    click.echo(click.style("\nCompliance Lineage Stats", bold=True))
    click.echo("=" * 30)
    click.echo(f"Total injections logged: {stats['total_injections']}")
    click.echo(f"Unique decisions surfaced: {stats['unique_decisions']}")
    click.echo(f"Sessions tracked: {stats['sessions']}")

    date_range = stats.get("date_range")
    if date_range:
        click.echo(f"Date range: {date_range['from']} to {date_range['to']}")

    most_surfaced = stats.get("most_surfaced", [])
    if most_surfaced:
        click.echo(click.style("\nMost frequently surfaced:", bold=True))
        for i, item in enumerate(most_surfaced, 1):
            click.echo(f"  {i}. {item['title'][:60]} (surfaced {item['count']} times)")


@main.command()
@click.option("--host", default="127.0.0.1", help="Host to bind to.")
@click.option("--port", default=0, help="Port (0 = auto-assign).")
def serve(host: str, port: int) -> None:
    """Start the SMM-Sync MCP server."""
    try:
        from smm_sync.mcp_server import run_server
    except ImportError as e:
        click.echo(click.style(f"MCP server unavailable: {e}", fg="red"))
        sys.exit(1)

    smm_dir = get_smm_dir()
    if not smm_dir.exists():
        click.echo(click.style("No .smm/ directory found. Run `smm init` first.", fg="red"))
        sys.exit(1)

    click.echo(click.style("Starting SMM-Sync MCP server...", fg="green"))
    run_server(smm_dir)


@main.command()
@click.option(
    "--period",
    default="week",
    type=click.Choice(["day", "week", "month"]),
    help="Time period for digest (default: week)",
)
@click.option(
    "--slack-webhook",
    default=None,
    envvar="CAAS_SLACK_WEBHOOK",
    help="Slack webhook URL. Set CAAS_SLACK_WEBHOOK env var to avoid passing every time.",
)
@click.option(
    "--json",
    "output_json",
    is_flag=True,
    default=False,
    help="Output as JSON instead of formatted text",
)
def digest(period: str, slack_webhook: str, output_json: bool) -> None:
    """Print a digest of CaaS activity for the period.

    Shows: decisions captured, architecture alerts, agent activity,
    estimated time saved, graph health.

    Post to Slack:
        smm digest --slack-webhook https://hooks.slack.com/...

    Or set env var:
        export CAAS_SLACK_WEBHOOK=https://hooks.slack.com/...
        smm digest

    Schedule weekly (add to crontab):
        0 9 * * 1 cd /your/project && smm digest --slack-webhook $CAAS_SLACK_WEBHOOK
    """
    import asyncio

    asyncio.run(_run_digest(period, slack_webhook, output_json))


async def _run_digest(period: str, slack_webhook: str | None, output_json: bool) -> None:
    """Run the digest command asynchronously.

    Args:
        period: 'day' | 'week' | 'month'.
        slack_webhook: Optional Slack webhook URL.
        output_json: If True, output JSON instead of formatted text.
    """
    import json as json_mod
    from dataclasses import asdict

    from smm_sync.digest import format_terminal, generate_digest, post_to_slack

    smm_dir = get_smm_dir()
    if not smm_dir.exists():
        click.echo("No .smm/ directory. Run smm init first.")
        return

    graph_client = None
    try:
        from smm_sync.context_graph.client import get_graph_client as _get_gc
        graph_client = _get_gc(graph_dir=smm_dir / "graph")
    except Exception:
        pass

    data = await generate_digest(smm_dir, graph_client, period)

    if output_json:
        click.echo(json_mod.dumps(asdict(data), default=str))
        return

    click.echo(format_terminal(data))

    if slack_webhook:
        await post_to_slack(slack_webhook, data)


@main.command()
@click.option("--host", default="127.0.0.1", help="Host to bind to.")
@click.option("--port", default=7842, help="Port to listen on (default: 7842).")
def dashboard(host: str, port: int) -> None:
    """Start the CaaS Dashboard web UI.

    Opens http://localhost:7842 in your browser.
    """
    try:
        from smm_sync.dashboard import run_dashboard
    except ImportError as e:
        click.echo(click.style(f"Dashboard unavailable: {e}", fg="red"))
        click.echo("Install: pip install fastapi uvicorn[standard] sse-starlette")
        sys.exit(1)

    if host != '127.0.0.1' and host != 'localhost':
        click.echo(
            click.style(
                f"\n\u26a0\ufe0f  WARNING: Binding to {host} exposes "
                f"your knowledge graph to the network.\n"
                f"   Anyone on your network can read your "
                f"architectural decisions and compliance logs.\n"
                f"   Only use this on trusted private networks.\n"
                f"   Default (safe): smm dashboard\n",
                fg="yellow",
            ),
            err=True,
        )
        if not click.confirm("Continue with network binding?"):
            click.echo("Aborted. Run: smm dashboard", err=True)
            return

    smm_dir = get_smm_dir()
    _load_keys_from_env_file(smm_dir)

    # Print CaaS startup banner
    project_name = find_project_root().name
    decision_count = 0
    contradiction_count = 0
    agent_count = 0
    try:
        from smm_sync.compliance.lineage import LineageLogger
        log_path = smm_dir / "compliance_lineage.jsonl"
        if log_path.exists():
            logger = LineageLogger(log_path)
            stats = logger.get_stats()
            agent_count = stats.get("sessions", 0)
    except Exception:
        pass

    # Time saved this week
    saved_week = _time_saved_week(smm_dir) if smm_dir.exists() else 0
    saved_h, saved_m = divmod(saved_week, 60)
    saved_str = f"~{saved_h}h {saved_m}m" if saved_h > 0 else f"~{saved_m}m"
    injection_count = int(saved_week / 3.75) if saved_week > 0 else 0

    click.echo(click.style("\nCaaS — Context as a Service", bold=True))
    click.echo("════════════════════════════")
    click.echo(f"URL:      http://{host}:{port}")
    click.echo(f"Project:  {project_name}")
    click.echo(f"Graph:    {decision_count} decisions · {contradiction_count} contradictions")
    click.echo(f"Agents:   {agent_count} active")
    click.echo()
    click.echo(f"⏱  Estimated time saved this week: {saved_str}")
    click.echo(f"   (Based on {injection_count} context injections × 3.75 min each)")
    click.echo("\nHow it works:")
    click.echo("  ① GitHub capture runs passively — no agent action required")
    click.echo("  ② Decisions inject automatically via MCP on every query_decisions call")
    click.echo("  ③ Everything runs locally — no data leaves your machine")
    click.echo("\nPress Ctrl+C to stop.\n")

    # Auto-open browser only in interactive terminal
    if sys.stdout.isatty():
        import webbrowser
        import threading
        import time
        def _open():
            time.sleep(1.2)
            webbrowser.open(f"http://{host}:{port}")
        threading.Thread(target=_open, daemon=True).start()

    run_dashboard(host=host, port=port)


# ---------------------------------------------------------------------------
# smm onboard
# ---------------------------------------------------------------------------

async def _generate_onboarding_doc(graph_client, project: str, api_key: str) -> str:
    """Generate ONBOARDING.md using Claude Haiku from graph decisions.

    Uses claude-haiku-4-5-20251001 to summarise decisions into a human-readable
    onboarding document. Falls back gracefully if the graph is empty or the API
    key is missing.

    Args:
        graph_client: GraphClient instance (may be None).
        project: Project name.
        api_key: Anthropic API key string.

    Returns:
        Markdown string for ONBOARDING.md.
    """
    decisions = []
    if graph_client is not None:
        try:
            decisions = await graph_client.get_decisions(project=project)
        except Exception:
            pass

    if not decisions:
        return (
            f"# Onboarding Guide — {project}\n\n"
            "> No decisions found in the context graph. "
            "Run `smm seed-graph` to populate it, then re-run `smm onboard`.\n"
        )

    # Build a prompt summary (cap at 30 decisions to stay within Haiku context)
    decision_lines = "\n".join(
        f"- **{d.title}**: {d.rationale[:200]}" for d in decisions[:30]
    )

    if not api_key:
        # Graceful degradation — produce a plain listing
        return (
            f"# Onboarding Guide — {project}\n\n"
            "> Generated without AI summary (no ANTHROPIC_API_KEY set).\n\n"
            "## Key Decisions\n\n"
            + decision_lines
            + "\n"
        )

    from datetime import date

    header = (
        f"# Onboarding Guide — {project}\n"
        f"<!-- Generated by smm onboard on {date.today().isoformat()} -->\n\n"
    )

    try:
        import anthropic

        client = anthropic.Anthropic(api_key=api_key)
        message = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=1024,
            messages=[
                {
                    "role": "user",
                    "content": (
                        f"You are a senior engineer writing an onboarding guide for new contributors "
                        f"to the '{project}' project.\n\n"
                        "Here are the key architectural decisions recorded in our context graph:\n\n"
                        f"{decision_lines}\n\n"
                        "Write a concise ONBOARDING.md (Markdown) that:\n"
                        "1. Explains what the project does in 2-3 sentences\n"
                        "2. Lists the 5 most important architectural decisions a new contributor MUST know\n"
                        "3. Lists 3 things NOT to do (the common mistakes)\n"
                        "4. Gives a quick-start checklist (3-5 items)\n"
                        "Keep it under 400 words. Use Markdown headers."
                    ),
                }
            ],
        )
        return header + message.content[0].text
    except Exception as exc:
        # Fallback on any API error
        return (
            header
            + f"> AI summary failed ({exc}). Listing raw decisions instead.\n\n"
            "## Key Decisions\n\n"
            + decision_lines
            + "\n"
        )


@main.command()
@click.option("--output", default="ONBOARDING.md", help="Output file path.")
@click.option("--project", default="", help="Project name (default: inferred).")
def onboard(output: str, project: str) -> None:
    """Generate an AI-powered ONBOARDING.md from the context graph.

    Uses Claude Haiku to summarise the project's architectural decisions into
    a human-readable guide for new contributors (~$0.003 per run).

    Commit the output — it's meant to be shared with the team.
    """
    import asyncio

    if not project:
        try:
            project = find_project_root().name
        except Exception:
            project = "smm-sync"

    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not api_key:
        click.echo(click.style(
            "Warning: ANTHROPIC_API_KEY not set — onboarding doc will be generated without AI summary.",
            fg="yellow",
        ))

    graph_client = None
    try:
        from smm_sync.context_graph.client import get_graph_client
        smm_dir = get_smm_dir()
        if not smm_dir.exists():
            click.echo(
                click.style(
                    "Warning: .smm/ not found. Run `smm init` first to initialise the project.",
                    fg="yellow",
                )
            )
        else:
            graph_client = get_graph_client(graph_dir=smm_dir / "graph")
    except Exception:
        click.echo(
            click.style(
                "Warning: Could not load graph client. Run `smm init` to initialise.",
                fg="yellow",
            )
        )

    doc = asyncio.run(_generate_onboarding_doc(graph_client, project, api_key))

    out_path = Path(output)
    out_path.write_text(doc, encoding="utf-8")
    click.echo(click.style(f"✓ {out_path} written ({len(doc)} chars)", fg="green"))
    click.echo("Commit it: git add ONBOARDING.md && git commit -m 'docs: regenerate onboarding guide'")


# ---------------------------------------------------------------------------
# smm install — helper functions
# ---------------------------------------------------------------------------

def _validate_anthropic_key(key: str) -> bool:
    """Make a minimal Anthropic API call to validate key.

    Uses the messages endpoint with max_tokens=1.
    Returns True if key is valid.
    Never raises — returns False on any error.

    Args:
        key: Anthropic API key to validate.

    Returns:
        True if valid, False otherwise.
    """
    try:
        import urllib.request
        import json as _json
        payload = _json.dumps({
            "model": "claude-haiku-4-5-20251001",
            "max_tokens": 1,
            "messages": [{"role": "user", "content": "hi"}]
        }).encode()
        req = urllib.request.Request(
            "https://api.anthropic.com/v1/messages",
            data=payload,
            headers={
                "x-api-key": key,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json"
            },
            method="POST"
        )
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status == 200
    except Exception:
        return False


def _validate_github_token(token: str) -> str | None:
    """Validate GitHub token and return username.

    Returns None if invalid.

    Args:
        token: GitHub personal access token to validate.

    Returns:
        GitHub username string if valid, None otherwise.
    """
    try:
        import urllib.request
        import json as _json
        req = urllib.request.Request(
            "https://api.github.com/user",
            headers={
                "Authorization": f"token {token}",
                "Accept": "application/vnd.github.v3+json",
                "User-Agent": "smm-sync/0.1.0",
            }
        )
        with urllib.request.urlopen(req, timeout=10) as r:
            data = _json.loads(r.read())
            return data.get("login")
    except Exception:
        return None


def _save_keys(smm_dir: Path, anthropic_key: str, github_token: str) -> None:
    """Save API keys to .smm/.env with restricted permissions.

    This file is gitignored by default.

    Args:
        smm_dir: Path to .smm/ directory.
        anthropic_key: Anthropic API key.
        github_token: GitHub personal access token.
    """
    env_path = smm_dir / ".env"
    env_path.write_text(
        f"ANTHROPIC_API_KEY={anthropic_key}\n"
        f"GITHUB_TOKEN={github_token}\n"
    )
    env_path.chmod(0o600)


def _load_keys_from_env_file(smm_dir: Path) -> None:
    """Load API keys from .smm/.env into os.environ if present.

    Sets os.environ for the current process only. Does not override
    existing environment variables.

    Args:
        smm_dir: Path to .smm/ directory.
    """
    env_path = smm_dir / ".env"
    if not env_path.exists():
        return
    for line in env_path.read_text().splitlines():
        if "=" in line and not line.startswith("#"):
            key, _, val = line.partition("=")
            os.environ.setdefault(key.strip(), val.strip())


def _update_gitignore(project_root: Path) -> None:
    """Add CaaS-related entries to .gitignore if not already present.

    Args:
        project_root: Root directory of the project.
    """
    gitignore = project_root / ".gitignore"
    entries = [
        ".smm/.env",
        ".smm/graph/",
        ".smm/capture_state.json",
        ".smm/compliance_lineage.jsonl",
        ".smm/killed_sessions.json",
        ".smm/board.json",
        ".smm/contradictions.jsonl",
    ]
    existing = ""
    if gitignore.exists():
        existing = gitignore.read_text()
    additions = [e for e in entries if e not in existing]
    if additions:
        with open(gitignore, "a") as f:
            f.write("\n# CaaS\n")
            for e in additions:
                f.write(f"{e}\n")


_AGENTS_MD_INSTALL_TEMPLATE = """\
# AGENTS.md — {name}

> Source of truth for this project. Read before writing any code.
> Managed by CaaS. Run `smm refresh` after editing.

## Project

{name}: Describe what this project does and why it exists.

## Architecture

### Example decision
**Why:** Reason it was made.

## Constraints

- Example: Python 3.11+ only

## Modules

- `main.py`: Entry point

## Active Task

Describe the current sprint or feature.
"""

# ---------------------------------------------------------------------------
# smm setup
# ---------------------------------------------------------------------------

_GITHUB_YML_TEMPLATE = """\
# .smm/github.yml
# GitHub capture configuration for CaaS
# Commit this file — it contains no secrets.

repos:
  - owner: {owner}
    name: {repo}
    project: {project}
    capture:
      pull_requests: true
      commits: true
      issues: true
      releases: true

settings:
  poll_interval_minutes: 30
  lookback_days: 30
  min_content_length: 50
  decision_keywords:
    - "decided"
    - "chose"
    - "rejected"
    - "because"
    - "instead of"
    - "rationale"
    - "trade-off"
    - "constraint"
    - "we will"
    - "we won't"
"""

_MCP_JSON_SNIPPET = """\
{
  "mcpServers": {
    "smm-sync": {
      "command": "smm",
      "args": ["serve"]
    }
  }
}"""


@main.command()
@click.option("--project", default="", help="Project name (default: inferred from directory).")
@click.option(
    "--skip-capture",
    is_flag=True,
    default=False,
    help="Skip the initial GitHub capture run.",
)
@click.option(
    "--skip-onboarding",
    is_flag=True,
    default=False,
    help="Skip generating ONBOARDING.md.",
)
def setup(project: str, skip_capture: bool, skip_onboarding: bool) -> None:
    """Interactive wizard to onboard a new repository to CaaS.

    Runs the full setup sequence in one command — no prior `smm init` needed:

    \b
    1. Scaffold AGENTS.md, .smm/, and pre-commit hook
    2. Detect git remote and infer GitHub owner/repo
    3. Check for required API keys (GITHUB_TOKEN, ANTHROPIC_API_KEY)
    4. Generate .smm/github.yml
    5. Run initial GitHub capture
    6. Generate ONBOARDING.md and print .mcp.json snippet

    All steps are idempotent — safe to re-run.
    """
    import asyncio

    cwd = Path.cwd()
    project_name = project or cwd.name

    click.echo(click.style("\nsmm setup — CaaS onboarding wizard", bold=True))
    click.echo("=" * 42)

    # ── 1. Scaffold project (init equivalent) ─────────────────────────────
    click.echo(click.style("\n[1/6] Scaffolding project...", fg="cyan"))

    smm_toml = cwd / "smm.toml"
    agents_md = cwd / "AGENTS.md"
    if smm_toml.exists():
        migrate_smm_toml(smm_toml, agents_md)

    if not agents_md.exists():
        agents_md.write_text(
            _AGENTS_MD_TEMPLATE.format(name=project_name),
            encoding="utf-8",
        )
        click.echo(f"  Created AGENTS.md for '{project_name}'")
    else:
        click.echo("  AGENTS.md already exists — keeping it.")

    smm_dir = cwd / ".smm"
    smm_dir.mkdir(exist_ok=True)
    (smm_dir / "locks").mkdir(exist_ok=True)
    (smm_dir / "history").mkdir(exist_ok=True)

    git_root = find_git_root(cwd)
    if git_root:
        if install_pre_commit_hook(git_root):
            click.echo("  Installed pre-commit hook.")
    else:
        click.echo("  Not in a git repo — skipping hook install.")

    click.echo(click.style("  ✓ Done", fg="green"))

    # ── 2. Detect git remote ──────────────────────────────────────────────
    click.echo(click.style("\n[2/6] Detecting git remote...", fg="cyan"))

    owner: str | None = None
    repo: str | None = None

    if git_root:
        parsed = get_git_remote(git_root)
        if parsed:
            owner, repo = parsed
            click.echo(
                click.style(f"  ✓ Detected GitHub remote: {owner}/{repo}", fg="green")
            )
        else:
            click.echo(
                click.style(
                    "  Could not detect GitHub remote from 'origin'. "
                    "You will need to edit .smm/github.yml manually.",
                    fg="yellow",
                )
            )
    else:
        click.echo(click.style("  Not in a git repository.", fg="yellow"))

    # Fall back to placeholder values so github.yml is still created
    owner = owner or "your-github-username"
    repo = repo or project_name

    # ── 3. Check required API keys ────────────────────────────────────────
    click.echo(click.style("\n[3/6] Checking API keys...", fg="cyan"))

    github_token = os.environ.get("GITHUB_TOKEN", "")
    anthropic_key = os.environ.get("ANTHROPIC_API_KEY", "")

    keys_ok = True
    if github_token:
        click.echo(click.style("  ✓ GITHUB_TOKEN is set", fg="green"))
    else:
        click.echo(
            click.style(
                "  ✗ GITHUB_TOKEN not set — required for GitHub capture.\n"
                "    Get a token at https://github.com/settings/tokens (scope: repo read)\n"
                "    Then run: export GITHUB_TOKEN=ghp_...",
                fg="red",
            )
        )
        keys_ok = False

    if anthropic_key:
        click.echo(click.style("  ✓ ANTHROPIC_API_KEY is set", fg="green"))
    else:
        click.echo(
            click.style(
                "  ✗ ANTHROPIC_API_KEY not set — required for decision extraction and onboarding.\n"
                "    Get your key at https://console.anthropic.com/settings/keys\n"
                "    Then run: export ANTHROPIC_API_KEY=sk-ant-...",
                fg="yellow",
            )
        )

    # ── 4. Generate .smm/github.yml ───────────────────────────────────────
    click.echo(click.style("\n[4/6] Generating .smm/github.yml...", fg="cyan"))

    github_yml = smm_dir / "github.yml"
    if github_yml.exists():
        click.echo(click.style("  .smm/github.yml already exists — skipping.", fg="yellow"))
    else:
        content = _GITHUB_YML_TEMPLATE.format(
            owner=owner,
            repo=repo,
            project=project_name,
        )
        github_yml.write_text(content, encoding="utf-8")
        click.echo(click.style(f"  ✓ Created .smm/github.yml for {owner}/{repo}", fg="green"))

    # ── 5. Initial GitHub capture ─────────────────────────────────────────
    click.echo(click.style("\n[5/6] Running initial GitHub capture...", fg="cyan"))

    if skip_capture:
        click.echo("  Skipped (--skip-capture).")
    elif not (github_token and anthropic_key):
        click.echo(
            click.style(
                "  Skipped — set GITHUB_TOKEN and ANTHROPIC_API_KEY first,\n"
                "  then run `smm capture run --once`.",
                fg="yellow",
            )
        )
    else:
        click.echo("  Fetching decisions from GitHub (this may take a minute)...")
        try:
            from smm_sync.capture import GitHubCapture
            from smm_sync.context_graph.client import GraphClient
        except ImportError as exc:
            click.echo(click.style(f"  Capture module unavailable: {exc}", fg="red"))
        else:
            graph_dir = smm_dir / "graph"
            state_path = smm_dir / "capture_state.json"
            graph_client = GraphClient(
                graph_dir=graph_dir, api_key=anthropic_key
            )
            capture = GitHubCapture(
                config_path=github_yml,
                state_path=state_path,
                graph_client=graph_client,
                github_token=github_token,
                api_key=anthropic_key,
            )
            try:
                asyncio.run(capture.run_once())
                click.echo(click.style("  ✓ Initial capture complete", fg="green"))
            except Exception as exc:
                click.echo(
                    click.style(f"  Capture failed: {exc}", fg="red")
                )

    # ── 6. Generate ONBOARDING.md ─────────────────────────────────────────
    click.echo(click.style("\n[6/6] Generating ONBOARDING.md...", fg="cyan"))

    if skip_onboarding:
        click.echo("  Skipped (--skip-onboarding).")
    else:
        graph_client_ob = None
        try:
            from smm_sync.context_graph.client import get_graph_client
            graph_client_ob = get_graph_client(graph_dir=smm_dir / "graph")
        except Exception:
            pass

        doc = asyncio.run(
            _generate_onboarding_doc(graph_client_ob, project_name, anthropic_key)
        )
        onboarding_path = cwd / "ONBOARDING.md"
        onboarding_path.write_text(doc, encoding="utf-8")
        click.echo(
            click.style(
                f"  ✓ ONBOARDING.md written ({len(doc)} chars)", fg="green"
            )
        )

    # ── 7. Print .mcp.json snippet ────────────────────────────────────────
    click.echo(click.style("\nAdd to your .mcp.json:", bold=True))
    click.echo(_MCP_JSON_SNIPPET)

    click.echo(click.style("\nSetup complete!", bold=True, fg="green"))
    click.echo("\nNext steps:")
    click.echo("  1. Edit AGENTS.md with your project's architecture decisions")
    click.echo("  2. Run `smm refresh` to parse AGENTS.md")
    if not keys_ok:
        click.echo(
            "  3. Set missing API keys above, then re-run `smm setup`\n"
            "     to populate the knowledge graph."
        )
    else:
        click.echo("  3. Run `smm capture run` to keep decisions synced from GitHub")
    click.echo("  4. Run `smm serve` to start the MCP server")


# ---------------------------------------------------------------------------
# smm install
# ---------------------------------------------------------------------------

@main.command("install")
def install() -> None:
    """Interactive setup wizard for CaaS.

    One command that does everything:
    1. Prompts for Anthropic API key (validates it)
    2. Prompts for GitHub token (validates it)
    3. Detects git remote automatically
    4. Creates .smm/ directory and config files
    5. Runs first GitHub capture
    6. Seeds knowledge graph
    7. Configures .mcp.json for Claude Code
    8. Prints next steps

    Keys are stored in .smm/.env (gitignored).
    Never written to any tracked file.
    """
    import asyncio as _asyncio
    import json as _json

    try:
        from smm_sync import __version__
    except Exception:
        __version__ = "0.1.0"

    W = 42

    def _banner(text: str = "") -> None:
        if text:
            click.echo(f"  {text}")
        else:
            click.echo("━" * W)

    _banner()
    click.echo("  CaaS — Context as a Service")
    click.echo(f"  v{__version__}")
    _banner()

    cwd = Path.cwd()
    click.echo(f"\n  Setting up CaaS for: {cwd.name}\n")

    # ── Step 1: Anthropic API Key ──────────────────────────────
    _banner()
    click.echo("  [1/2] Anthropic API Key")
    _banner()
    click.echo()
    click.echo("  CaaS uses Claude Haiku to extract decisions")
    click.echo("  from your GitHub PRs. You need an Anthropic")
    click.echo("  API key to enable this.")
    click.echo()
    click.echo("  Get yours at:")
    click.echo("  → https://console.anthropic.com/settings/keys")
    click.echo()

    anthropic_key = ""
    while not anthropic_key:
        key = click.prompt("  Paste your key (sk-ant-...)", hide_input=True)
        click.echo("  Validating...", nl=False)
        if _validate_anthropic_key(key):
            anthropic_key = key
            click.echo(click.style(" ✓ Key validated (claude-haiku-4-5-20251001 ✓)", fg="green"))
        else:
            click.echo(click.style(" ✗ Invalid key. Try again.", fg="red"))

    # ── Step 2: GitHub Token ──────────────────────────────────
    click.echo()
    _banner()
    click.echo("  [2/2] GitHub Token")
    _banner()
    click.echo()
    click.echo("  CaaS reads your GitHub PRs to capture")
    click.echo("  architectural decisions. You need a personal")
    click.echo("  access token with repo read access.")
    click.echo()
    click.echo("  Get yours at:")
    click.echo("  → https://github.com/settings/tokens/new")
    click.echo("    (select scope: repo → read)")
    click.echo()

    github_token = ""
    github_user = ""
    while not github_token:
        token = click.prompt("  Paste your token (ghp_...)", hide_input=True)
        click.echo("  Validating...", nl=False)
        username = _validate_github_token(token)
        if username:
            github_token = token
            github_user = username
            click.echo(click.style(f" ✓ Token validated ({username} ✓)", fg="green"))
        else:
            click.echo(click.style(" ✗ Invalid token. Try again.", fg="red"))

    # ── Setup ─────────────────────────────────────────────────
    click.echo()
    _banner()
    click.echo("  Setting up your project...")
    _banner()
    click.echo()

    os.environ["ANTHROPIC_API_KEY"] = anthropic_key
    os.environ["GITHUB_TOKEN"] = github_token

    # Init .smm/ directory
    smm_dir = cwd / ".smm"
    smm_dir.mkdir(exist_ok=True)
    (smm_dir / "locks").mkdir(exist_ok=True)
    (smm_dir / "history").mkdir(exist_ok=True)

    # Save keys
    _save_keys(smm_dir, anthropic_key, github_token)
    _update_gitignore(cwd)

    # Detect git remote
    owner, repo = "your-org", cwd.name
    git_root = find_git_root(cwd)
    if git_root:
        parsed = get_git_remote(git_root)
        if parsed:
            owner, repo = parsed

    click.echo(click.style(f"  ✓ Detected GitHub remote: {owner}/{repo}", fg="green"))

    # Create .smm/github.yml
    github_yml = smm_dir / "github.yml"
    if not github_yml.exists():
        github_yml.write_text(
            _GITHUB_YML_TEMPLATE.format(owner=owner, repo=repo, project=cwd.name)
        )
    click.echo(click.style("  ✓ Created .smm/github.yml", fg="green"))

    # Create AGENTS.md
    agents_md = cwd / "AGENTS.md"
    if not agents_md.exists():
        agents_md.write_text(_AGENTS_MD_INSTALL_TEMPLATE.format(name=cwd.name))
    click.echo(click.style("  ✓ Created AGENTS.md", fg="green"))

    # Run first capture
    click.echo("  ✓ Running first capture...")
    decisions = 0
    try:
        from smm_sync.capture import GitHubCapture
        from smm_sync.context_graph.client import GraphClient as _GC
        graph_client = _GC(graph_dir=smm_dir / "graph", api_key=anthropic_key)
        capture = GitHubCapture(
            config_path=github_yml,
            state_path=smm_dir / "capture_state.json",
            graph_client=graph_client,
            github_token=github_token,
            api_key=anthropic_key,
        )
        result = _asyncio.run(capture.run_once())
        decisions = result.get("decisions_captured", 0) if isinstance(result, dict) else 0
        click.echo(click.style(f"    Found {decisions} decisions", fg="green"))
    except Exception as exc:
        click.echo(click.style(f"  ⚠ Capture failed: {exc}", fg="yellow"))

    # Configure .mcp.json
    mcp_path = cwd / ".mcp.json"
    if not mcp_path.exists():
        mcp_config = {
            "mcpServers": {
                "caas": {
                    "command": "smm",
                    "args": ["serve"]
                }
            }
        }
        mcp_path.write_text(_json.dumps(mcp_config, indent=2))
        click.echo(click.style("  ✓ Configured .mcp.json", fg="green"))

    # ── Done ──────────────────────────────────────────────────
    click.echo()
    _banner()
    click.echo(click.style("  CaaS is ready.", bold=True))
    _banner()
    click.echo()
    click.echo("  Dashboard:  smm dashboard")
    click.echo("  MCP server: smm serve")
    click.echo('  Query:      smm query "why did we reject X"')
    click.echo()
    click.echo("  Add to Claude Code: restart Claude Code in")
    click.echo("  this directory — .mcp.json is already set up.")
    click.echo()
    if decisions > 0:
        click.echo(click.style(f"  ⏱  Your team made {decisions} decisions.", fg="cyan"))
        click.echo(click.style("     Your AI agents now know all of them.", fg="cyan"))
    _banner()
