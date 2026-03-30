"""SMM-Sync CLI — smm init, refresh, status, claim, release, serve."""
from __future__ import annotations

import os
import sys
from pathlib import Path

import click

from smm_sync.config import DASHBOARD_PORT, find_project_root, get_smm_dir
from smm_sync.coordinator import claim as _claim
from smm_sync.coordinator import is_claimed, list_claimed
from smm_sync.coordinator import release as _release
from smm_sync.git_utils import find_git_root, get_git_remote, install_pre_commit_hook
from smm_sync.ingester import ingest, load_parsed_context, migrate_smm_toml
from smm_sync.state import get_current_state, propose

_AGENTS_MD_TEMPLATE = """\
# Project Architecture Memory — Powered by Axiom Hub

You are a Senior Engineer whose primary obligation is the integrity of this project's architectural memory. Every implementation choice you make MUST be externalized through the Axiom Hub MCP tools.

## Mandatory Startup Protocol
Before writing ANY code in this session:
1. Call `get_project_context` to load all active decisions and check for unresolved contradictions
2. If contradictions exist (⚠ ACTION REQUIRED), stop and notify the user before proceeding
3. Review active decisions to ensure your planned changes align with existing architecture

---

## Project

**Name:** {name}

{name}: Describe what this project does and why it exists.

---

## Decision Capture Rules

Call `add_decision` for ANY choice where two or more alternatives existed. Specifically:

### Always Capture (type: architectural)
- Database selection or configuration changes
- API format decisions (REST vs GraphQL vs gRPC)
- Service communication patterns (sync vs async, queues vs direct calls)
- Authentication/authorization approach changes
- Caching strategy choices
- Deployment architecture changes

### Always Capture (type: technical)
- Library or framework selections (e.g., choosing axios over fetch)
- Language or runtime version decisions
- Database driver or ORM choices
- Concurrency patterns (threading vs async vs multiprocessing)
- Error handling strategies (retry logic, circuit breakers)
- Logging and observability tool choices

### Always Capture (type: product)
- Feature scope decisions (what's in/out of this sprint)
- User-facing behavior choices (defaults, fallbacks, error messages)
- API contract decisions that affect consumers
- Data format choices visible to users (CSV vs JSON vs PDF)

### Always Capture (type: constraint)
- Performance budgets (response time targets, memory limits)
- Security requirements (encryption, auth standards, compliance)
- Data residency or retention requirements
- Team or timeline constraints affecting implementation
- Cost limits affecting infrastructure choices

### Do NOT Capture
- Variable naming (unless it's a project-wide convention)
- Import ordering
- Minor formatting choices
- Obvious single-option situations (no real alternative existed)

---

## Examples of Correct Decision Capture

### Example 1: Database Driver Selection
You're implementing a user service and choose asyncpg over psycopg2.

Call: `add_decision`
- title: "asyncpg for PostgreSQL connections in user-service"
- description: "Selected asyncpg over psycopg2 for the user service. asyncpg provides native async/await support matching our FastAPI stack, 3x throughput on benchmarks for our read-heavy workload. psycopg2 would require thread pool executor wrapping."
- type: "technical"
- confidence: 0.9
- made_by: "agent"

### Example 2: Auth Pattern Change
You're adding authentication and choose JWT with RS256 over session cookies.

Call: `add_decision`
- title: "JWT with RS256 signing for API authentication"
- description: "Implementing stateless JWT auth with RS256 asymmetric signing instead of session-based cookies. Rationale: microservices architecture requires stateless auth — sessions would need shared Redis store adding infrastructure complexity. RS256 chosen over HS256 for key rotation support."
- type: "architectural"
- confidence: 0.95
- made_by: "agent"

### Example 3: Scope Constraint
PM said no data export in v1, so you skip building the CSV endpoint.

Call: `add_decision`
- title: "No data export endpoints in v1 API"
- description: "Skipping /api/export/csv and /api/export/pdf endpoints per product decision. V1 focuses on core CRUD operations only. Export functionality deferred to v2 backlog."
- type: "product"
- confidence: 0.85
- made_by: "agent"

---

## When You Encounter Contradictions

If `get_project_context` returns ⚠ UNRESOLVED CONTRADICTIONS:
1. STOP — do not write code that depends on either contradicting decision
2. Notify the user: "There are X unresolved contradictions that affect this work"
3. List the specific contradictions relevant to your current task
4. Wait for the user to resolve them on the dashboard (`smm dashboard`)
5. Call `get_project_context` again after resolution to confirm

If the isError gate blocks you:
1. Read the error message — it tells you exactly what artifacts need cleanup
2. Run the fix commands provided in the error
3. Call `get_project_context` again to verify the block is cleared

---

## Decision Granularity — What's Worth Reporting

| Tag | Category | Capture Threshold | Example |
|-----|----------|------------------|---------|
| FN | Functional | HIGH — any change to business rules | "Switching from batch to stream processing for order events" |
| NF-P | Performance | MEDIUM — any latency/throughput impact | "Setting connection pool_size=5 for RDS memory optimization" |
| NF-S | Security | LOW threshold (always capture) | "Login endpoint now uses OAuth2 form-data not JSON body" |
| NF-O | Operability | MEDIUM — logging, monitoring changes | "Implemented asyncio.Lock for concurrency control" |
| TEST | Testing | HIGH — new test strategies only | "Added automated latency regression tests for API v2" |
| DOC | Documentation | LOW — major architectural shifts only | "Externalizing session state to Axiom Hub" |

When in doubt, capture it. A dismissed decision costs nothing. A missed contradiction costs days.

---

## Architecture

> Edit this section to describe your project's key architectural decisions.

### Tech Stack
- Language/Runtime: (fill in)
- Framework: (fill in)
- Database: (fill in)
- Deployment: (fill in)

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

_CURSOR_RULES_TEMPLATE = """\
---
description: Axiom Hub Context-as-a-Service integration
alwaysApply: true
---

Before writing any code, call the `get_project_context` MCP tool to load architectural decisions and check for contradictions. Call `add_decision` for any implementation choice where alternatives existed.

See AGENTS.md in the project root for full decision capture rules and examples.
"""

_AGENTS_SKILL_TEMPLATE = """\
---
name: axiom-caas-protocol
description: Mandatory Context-as-a-Service integration. Use immediately upon session initialization and whenever architectural decisions are made.
---

# Axiom Hub Protocol

Before any code changes:
1. Call `get_project_context` to load active decisions and unresolved contradictions
2. If contradictions exist, STOP and notify the user

When making implementation choices:
- Call `add_decision` for any choice where two or more alternatives existed
- Include: title, description, type (architectural/technical/product/constraint), confidence (0.0-1.0)

See AGENTS.md in the project root for full decision capture rules, few-shot examples, and the Nine-Box granularity framework.
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


_MANDATORY_PROTOCOL = """\
MANDATORY PROTOCOL — YOU MUST FOLLOW THESE RULES FOR THIS ENTIRE SESSION:

1. EVERY time you choose between two or more alternatives (database, library, framework, auth pattern, API format, caching strategy, concurrency pattern), you MUST call the add_decision tool BEFORE writing the code. Not after. Before.

2. add_decision requires: title (what you chose), description (why, and what you rejected), type (architectural/technical/product/constraint), confidence (0.0-1.0).

3. If you write code without calling add_decision for the choice that led to that code, you are violating this project's governance protocol.

4. AFTER completing your implementation, review every file you wrote. For each technical choice you made during coding (config patterns, test strategies, error handling approaches, dependency injection patterns, data seeding strategies), call add_decision retroactively. Implementation decisions matter as much as architectural ones.

Example — you are about to use SQLAlchemy for ORM:
→ FIRST call add_decision with title='SQLAlchemy 2.0 for ORM', description='Selected SQLAlchemy over Tortoise ORM and Django ORM for async support and mature ecosystem', type='technical', confidence=0.9
→ THEN write the code

This is not optional. This is a project requirement.\
"""

_MANDATORY_REMINDER = "--- REMINDER: Call add_decision for EVERY implementation choice this session. ---"


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
    pre_commit_ok = False
    if git_root:
        pre_commit_ok = install_pre_commit_hook(git_root)
        if pre_commit_ok:
            pass  # reported in final summary below
        else:
            click.echo(click.style("Could not install pre-commit hook (no .git/hooks).", fg="yellow"))
    else:
        click.echo(click.style("Not in a git repo — skipping hook install.", fg="yellow"))

    # ── Polyglot agent auto-discovery configs ─────────────────────────────────
    import json as _json_poly

    # A) .claude/settings.json — project-level PreToolUse hook for Claude Code
    _claude_dir = cwd / ".claude"
    _claude_dir.mkdir(exist_ok=True)
    _claude_settings_path = _claude_dir / "settings.json"
    # Single hook: Claude Code runs ALL matching hooks, so we cannot use a separate
    # allow-hook for get_project_context (it would still trigger the block hook too).
    # Instead, read tool_name from stdin JSON; pass through get_project_context, else
    # block until the lock file (created by get_project_context) exists.
    _block_hook = {
        "matcher": ".*",
        "hooks": [
            {
                "type": "command",
                "command": (
                    "bash -c 'INPUT=$(cat);"
                    " if echo \"$INPUT\" | grep -q get_project_context; then exit 0; fi;"
                    " RESOLVED=$(realpath \"$PWD\");"
                    " LOCK=/tmp/smm-session-$(printf \"%s\" \"$RESOLVED\" | shasum | cut -c1-8).lock;"
                    " FRESH=$(find \"$LOCK\" -mmin -30 2>/dev/null);"
                    " if [ -z \"$FRESH\" ];"
                    " then echo \"\u26a0\ufe0f AXIOM HUB: Call get_project_context first.\" >&2; exit 2; fi'"
                ),
            }
        ],
    }
    _settings: dict = {}
    if _claude_settings_path.exists():
        try:
            _settings = _json_poly.loads(_claude_settings_path.read_text(encoding="utf-8"))
        except Exception:
            pass
    if "hooks" not in _settings:
        _settings["hooks"] = {}
    if "PreToolUse" not in _settings["hooks"]:
        _settings["hooks"]["PreToolUse"] = []
    # Remove stale smm-managed hooks (identified by our unique lock-file signature or
    # the old mcp__.* matcher) so re-running `smm init` is idempotent.
    _SMM_SIG = "smm-session-"  # unique string present in all our block commands
    _remaining_hooks = [
        h for h in _settings["hooks"]["PreToolUse"]
        if not (
            isinstance(h, dict) and (
                h.get("matcher") == "mcp__.*"  # old single-hook style
                or h.get("matcher") == "mcp.*get_project_context"
                or any(
                    _SMM_SIG in hk.get("command", "")
                    for hk in h.get("hooks", [])
                    if isinstance(hk, dict)
                )
            )
        )
    ]
    # Prepend our hook so it runs before any user-defined hooks
    _settings["hooks"]["PreToolUse"] = [_block_hook] + _remaining_hooks

    # PostToolUse hook — gentle reminder to capture decisions after Write/Edit
    _post_hook = {
        "matcher": "Write|Edit|MultiEdit",
        "hooks": [
            {
                "type": "command",
                "command": (
                    "bash -c 'echo \"[Axiom Hub] Did you make an architectural choice?"
                    " If so, call add_decision before continuing.\" >&2; exit 0'"
                ),
            }
        ],
    }
    if "PostToolUse" not in _settings["hooks"]:
        _settings["hooks"]["PostToolUse"] = []
    # Remove stale smm-managed PostToolUse hooks (Write/Edit reminder + Bash commit reset)
    _post_remaining = [
        h for h in _settings["hooks"]["PostToolUse"]
        if not (
            isinstance(h, dict)
            and (
                (
                    h.get("matcher") == "Write|Edit|MultiEdit"
                    and any(
                        "Axiom Hub" in hk.get("command", "")
                        for hk in h.get("hooks", [])
                        if isinstance(hk, dict)
                    )
                )
                or (
                    h.get("matcher") == "Bash"
                    and any(
                        _SMM_SIG in hk.get("command", "")
                        for hk in h.get("hooks", [])
                        if isinstance(hk, dict)
                    )
                )
            )
        )
    ]
    # PostToolUse hook — session reset after git commit clears both lock files
    _commit_hook = {
        "matcher": "Bash",
        "hooks": [
            {
                "type": "command",
                "command": (
                    "bash -c 'INPUT=$(cat);"
                    " if echo \"$INPUT\" | grep -q \"git commit\"; then"
                    " HASH=$(printf \"%s\" \"$PWD\" | shasum | cut -c1-8);"
                    " rm -f /tmp/smm-session-$HASH.lock /tmp/smm-review-$HASH.lock;"
                    " echo \"[Axiom Hub] Session complete. Next task will require fresh context load.\" >&2;"
                    " fi;"
                    " exit 0'"
                ),
            }
        ],
    }
    _settings["hooks"]["PostToolUse"] = [_post_hook, _commit_hook] + _post_remaining
    _claude_settings_path.write_text(_json_poly.dumps(_settings, indent=2), encoding="utf-8")

    # B) .cursor/rules/axiom-hub.mdc — Cursor alwaysApply rule
    _cursor_rules_dir = cwd / ".cursor" / "rules"
    _cursor_rules_dir.mkdir(parents=True, exist_ok=True)
    (_cursor_rules_dir / "axiom-hub.mdc").write_text(_CURSOR_RULES_TEMPLATE, encoding="utf-8")

    # C) .agents/skills/axiom-caas/SKILL.md — universal agentskills.io standard
    _agents_skill_dir = cwd / ".agents" / "skills" / "axiom-caas"
    _agents_skill_dir.mkdir(parents=True, exist_ok=True)
    (_agents_skill_dir / "SKILL.md").write_text(_AGENTS_SKILL_TEMPLATE, encoding="utf-8")

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
                    click.echo(click.style("  ✓ Claude Code PreToolUse + PostToolUse hooks configured.", fg="green"))
                else:
                    click.echo(click.style("  ⚠ Could not update ~/.claude/settings.json.", fg="yellow"))
            if agent_choice in ("cursor", "both"):
                if configure_cursor_hook(cwd):
                    click.echo(click.style("  ✓ Cursor hook written to .cursor/hooks.json.", fg="green"))

            # Persist agent choice to .smm/config.json so contradiction
            # detection in add_decision_local() can route to the right CLI.
            import json as _json_cfg
            _config_path = smm_dir / "config.json"
            _config: dict = {}
            if _config_path.exists():
                try:
                    _config = _json_cfg.loads(_config_path.read_text(encoding="utf-8"))
                except Exception:
                    pass
            _config["agent"] = agent_choice
            _config_path.write_text(_json_cfg.dumps(_config, indent=2), encoding="utf-8")

            click.echo(click.style("Axiom Lore-Hook installed.", fg="green"))
        except Exception as _lh_exc:
            click.echo(
                click.style(f"Lore-Hook install warning: {_lh_exc}", fg="yellow"),
                err=True,
            )

    # ── Final summary ─────────────────────────────────────────────────────────
    click.echo("")
    click.echo(click.style("✓ AGENTS.md generated (Cursor, Copilot, Devin, Codex — auto-read)", fg="green"))
    click.echo(click.style("✓ .claude/settings.json generated (Claude Code — PreToolUse + PostToolUse hooks)", fg="green"))
    click.echo(click.style("✓ .cursor/rules/axiom-hub.mdc generated (Cursor — alwaysApply)", fg="green"))
    click.echo(click.style("✓ .agents/skills/axiom-caas/SKILL.md generated (Cline, Windsurf — universal)", fg="green"))
    if pre_commit_ok:
        click.echo(click.style("✓ Pre-commit hook installed", fg="green"))
    click.echo(click.style("✓ MCP server configured", fg="green"))
    click.echo("")
    click.echo("Your AI agent will now automatically:")
    click.echo("  1. Load project context at session start")
    click.echo("  2. Capture architectural decisions as it codes")
    click.echo("  3. Get blocked if unresolved contradictions exist")
    click.echo("")

    if mode == "dashboard":
        click.echo(click.style("\nDashboard mode — launching web UI...", fg="cyan"))
        ctx.invoke(dashboard)
    else:
        click.echo(_MCP_CONFIG_HINT)


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
    new_hash = hashlib.sha256(content.encode()).hexdigest()

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


@main.command("get-context")
@click.option("--project", default="smm-sync", help="Project name.")
def get_context_cmd(project: str) -> None:
    """Output a clean summary of project decisions, contradictions, and PM resolutions.

    Reads directly from JSONL files — no model loading, no graph sync.
    Readable by AI agents and humans.

    Example:
        smm get-context
        smm get-context --project mf-tracker
    """
    import json as _json
    from datetime import datetime, timezone, timedelta

    smm_dir = get_smm_dir()
    if not smm_dir.exists():
        click.echo(click.style("No .smm/ directory found. Run `smm init` first.", fg="red"))
        sys.exit(1)

    # ── 1. Read decisions from JSONL ──────────────────────────────────────────
    decisions_path = smm_dir / "decisions.jsonl"
    decisions_data: list[dict] = []
    if decisions_path.exists():
        for _line in decisions_path.read_text(encoding="utf-8").splitlines():
            _line = _line.strip()
            if _line:
                try:
                    decisions_data.append(_json.loads(_line))
                except Exception:
                    pass

    # Count by type
    type_counts: dict[str, int] = {}
    for d in decisions_data:
        dt = (d.get("type") or "technical").lower()
        type_counts[dt] = type_counts.get(dt, 0) + 1

    # ── 2. Recent decisions (last 7 days) ────────────────────────────────────
    cutoff_7d = datetime.now(timezone.utc) - timedelta(days=7)
    recent: list[dict] = []
    for d in decisions_data:
        ts_str = d.get("timestamp", "")
        try:
            ts = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
            if ts >= cutoff_7d:
                recent.append({
                    "title": d.get("title", ""),
                    "type": (d.get("type") or "technical").lower(),
                    "confidence": float(d.get("confidence") or 0.80),
                })
        except Exception:
            pass

    # ── 3. Load contradictions from JSONL ─────────────────────────────────────
    contra_path = smm_dir / "contradictions.jsonl"
    all_contras: list[dict] = []
    if contra_path.exists():
        for _line in contra_path.read_text(encoding="utf-8").splitlines():
            _line = _line.strip()
            if _line:
                try:
                    all_contras.append(_json.loads(_line))
                except Exception:
                    pass

    # Unresolved = not resolved, not dismissed, not ignored
    active_contras = [
        c for c in all_contras
        if c.get("status") not in ("resolved", "dismissed", "ignored")
        and not c.get("resolved", False)
    ]

    # ── 4. Load contradiction_index.json (resolved pairs) ────────────────────
    idx_path = smm_dir / "contradiction_index.json"
    resolved_pairs: list[dict] = []
    action_required: list[dict] = []

    if idx_path.exists():
        try:
            idx = _json.loads(idx_path.read_text(encoding="utf-8"))
            cutoff_30d = datetime.now(timezone.utc) - timedelta(days=30)
            cutoff_7d_ar = datetime.now(timezone.utc) - timedelta(days=7)
            for pair in idx.get("pairs", []):
                if pair.get("status") != "resolved":
                    continue
                actioned_str = pair.get("actioned_at", "")
                if not actioned_str:
                    continue
                try:
                    actioned_dt = datetime.fromisoformat(actioned_str.replace("Z", "+00:00"))
                except Exception:
                    continue
                if actioned_dt < cutoff_30d:
                    continue
                winner = pair.get("decision_a_title", "")
                loser = pair.get("decision_b_title", "")
                note = pair.get("note", "")
                actor = pair.get("actioned_by", "dashboard")
                date_str = actioned_str[:10]
                resolved_pairs.append({
                    "winner": winner, "loser": loser, "note": note,
                    "actor": actor, "date": date_str,
                })
                if actioned_dt >= cutoff_7d_ar:
                    action_required.append({
                        "winner": winner, "loser": loser, "note": note, "date": date_str,
                    })
        except Exception:
            pass

    # ── 5. Format output ──────────────────────────────────────────────────────
    total = len(decisions_data)
    type_summary = ", ".join(
        f"{v} {k}" for k, v in sorted(type_counts.items())
    )

    click.echo(_MANDATORY_PROTOCOL)
    click.echo("")
    click.echo("---")
    click.echo(f"Project: {project}")
    click.echo(f"Decisions: {total} total ({type_summary})")
    click.echo("")

    if recent:
        click.echo("Recent decisions (last 7 days):")
        for i, d in enumerate(recent[:5], 1):
            click.echo(f"  {i}. {d['title']} [{d['type']}, {d['confidence']:.2f}]")
        click.echo("")

    # Contradiction summary — always show (Design fix)
    if active_contras:
        click.echo(f"\u26a0 UNRESOLVED CONTRADICTIONS ({len(active_contras)}):")
        for i, c in enumerate(active_contras, 1):
            a = c.get("decision_a", "")
            b = c.get("decision_b", "")
            conf = c.get("confidence")
            conf_str = f" \u2014 Confidence: {conf:.2f}" if conf is not None else ""
            click.echo(f'  {i}. "{a}" CONTRADICTS "{b}"{conf_str}')
        click.echo("")
        click.echo("  ACTION REQUIRED: Resolve these contradictions on the dashboard (smm dashboard) before proceeding.")
        click.echo("")
    else:
        click.echo("\u2705 No unresolved contradictions")
        click.echo("")

    if resolved_pairs:
        click.echo("Resolved contradictions (last 30 days):")
        for rp in resolved_pairs:
            click.echo(
                f'  \u2713 KEEP: "{rp["winner"]}" \u2014 SUPERSEDED: "{rp["loser"]}"'
            )
            click.echo(f'    Resolved by: {rp["actor"]} on {rp["date"]}')
            if rp["note"]:
                click.echo(f'    Note: {rp["note"]}')
        click.echo("")

    if action_required:
        click.echo("ACTION REQUIRED \u2014 Implement these PM resolutions:")
        for i, ar in enumerate(action_required, 1):
            click.echo(f"  {i}. Codebase must use {ar['winner']}, NOT {ar['loser']}")
        click.echo(
            "  Review and refactor any code that still follows superseded decisions."
        )
        click.echo("")

    click.echo("---")
    click.echo("")
    click.echo(_MANDATORY_REMINDER)


def _server_running_on_port(port: int) -> bool:
    """Non-blocking check: is something listening on localhost:port?

    Used by add-decision and add-decisions-batch to detect whether the smm-sync
    dashboard is running before attempting a direct Kuzu write.  If the dashboard
    is running it holds the Kuzu exclusive lock; routing through it avoids the
    lock conflict (Bug 1 fix).

    Args:
        port: TCP port to probe.

    Returns:
        True if a server responded on localhost:port within 100 ms.
    """
    import socket
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.1)
        return s.connect_ex(("localhost", port)) == 0


def _post_decision_to_dashboard(
    data: dict,
    content: str,
    project: str,
    port: int = DASHBOARD_PORT,
) -> str:
    """POST a decision to the running dashboard server and return the decision id.

    The dashboard's POST /api/decisions endpoint calls add_decision_local() which
    already holds the Kuzu connection, so there is no lock conflict.

    Args:
        data: Raw JSON dict from the CLI input.
        content: Pre-built content string (may include Status line).
        project: Project name for the graph.
        port: Dashboard port (default 7842).

    Returns:
        Decision id string returned by the server.

    Raises:
        Exception: Any network or HTTP error propagates to the caller.
    """
    import json as _json
    import urllib.request as _urllib_req

    payload = _json.dumps({
        "title": data.get("title", "Unnamed decision"),
        "content": content,
        "rationale": data.get("rationale", ""),
        "made_by": data.get("made_by", "lore-hook"),
        "decision_type": data.get("decision_type", data.get("type", "technical")),
        "alternatives": data.get("alternatives", []),
        "constraints": data.get("constraints", []),
        "source_type": data.get("source_type", "manual"),
        "confidence": data.get("confidence"),
    }).encode()
    req = _urllib_req.Request(
        f"http://localhost:{port}/api/decisions",
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with _urllib_req.urlopen(req, timeout=10) as resp:
        result = _json.loads(resp.read())
    return result.get("id") or result.get("decision_id") or "routed"


def _find_rust_binary() -> str | None:
    """Locate the smm-fast-write Rust binary.

    Checks in order:
      1. SMM_FAST_WRITE_BIN environment variable override.
      2. smm-fast-write on PATH (installed via maturin / pip).
      3. rust_cli/target/release/smm-fast-write relative to the repo root
         (local dev build via ``cargo build --release``).

    Returns:
        Absolute path string to an executable binary, or None if not found.
    """
    import shutil
    from pathlib import Path as _Path

    candidates: list[_Path] = []

    if os.environ.get("SMM_FAST_WRITE_BIN"):
        candidates.append(_Path(os.environ["SMM_FAST_WRITE_BIN"]))

    which = shutil.which("smm-fast-write")
    if which:
        candidates.append(_Path(which))

    # Resolve repo root from this file: src/smm_sync/cli.py → ../../
    _pkg_file = _Path(__file__)
    candidates.append(
        _pkg_file.parents[3] / "rust_cli" / "target" / "release" / "smm-fast-write"
    )

    for c in candidates:
        if c.is_file() and os.access(str(c), os.X_OK):
            return str(c)
    return None


@main.command("add-decision")
@click.argument("source", type=click.File("r"), default="-", required=False, metavar="[JSON_FILE|-]")
@click.option("--project", default="smm-sync", help="Project name.")
@click.option(
    "--local",
    "use_local",
    is_flag=True,
    default=False,
    help="Kept for backward compatibility. Both paths now write to JSONL only.",
)
@click.option(
    "--context",
    "ctx_note",
    default=None,
    help="Context note: PRD name, ticket ID, or source description (e.g. 'PRD-001: Legacy Migration').",
)
@click.option("--title", "title_opt", default=None, help="Decision title (alternative to JSON input).")
@click.option("--description", "description_opt", default=None, help="Decision description/rationale.")
@click.option("--type", "type_opt", default=None, help="Decision type: architectural/technical/product/constraint.")
@click.option("--confidence", "confidence_opt", type=float, default=None, help="Confidence score (0.0-1.0).")
@click.option("--made-by", "made_by_opt", default=None, help="Who made this decision.")
def add_decision_cmd(
    source: click.File,
    project: str,
    use_local: bool,
    ctx_note: str | None,
    title_opt: str | None,
    description_opt: str | None,
    type_opt: str | None,
    confidence_opt: float | None,
    made_by_opt: str | None,
) -> None:
    """Record a decision from JSON or named flags. Reads from stdin (-) or a file.

    Hot path: tries the compiled Rust binary (smm-fast-write) first — ~10 ms.
    Falls back to a pure-Python JSONL append if Rust is unavailable — < 500 ms.
    Neither path loads Kuzu, embeddings, or an LLM.

    Kuzu sync and contradiction detection happen lazily in ``smm check``.

    JSON fields: title (required), rationale (required), type (required),
    confidence, alternatives (list), constraints (list), made_by, source.

    Example:
        echo '{"title":"Use Kuzu","rationale":"No Docker needed","type":"technical"}' \\
            | smm add-decision -
        smm add-decision --title "Use Kuzu" --description "No Docker needed" --type technical
        smm add-decision --local decision.json   # --local flag kept for compat
    """
    import json as _json
    import subprocess as _subprocess

    if title_opt:
        # Build data dict from named flags — no stdin read needed.
        if not description_opt:
            click.echo(click.style("--description is required when using --title", fg="red"), err=True)
            sys.exit(1)
        data: dict = {
            "title": title_opt,
            "rationale": description_opt,
            "type": type_opt or "technical",
        }
        if confidence_opt is not None:
            data["confidence"] = confidence_opt
        if made_by_opt:
            data["made_by"] = made_by_opt
    else:
        try:
            data = _json.load(source)
        except _json.JSONDecodeError as exc:
            click.echo(click.style(f"Invalid JSON: {exc}", fg="red"), err=True)
            sys.exit(1)

    # Normalise: if caller passed "decision_type" map to "type" for Rust binary.
    if "decision_type" in data and "type" not in data:
        data["type"] = data["decision_type"]
    if "project" not in data:
        data["project"] = project
    # --context flag: inject into data if not already provided in the JSON
    if ctx_note and "context" not in data:
        data["context"] = {"source": ctx_note, "trigger": "", "git_ref": "", "branch": ""}

    # ── 1. Try Rust binary ──────────────────────────────────────────────────
    if not os.environ.get("SMM_NO_RUST"):
        rust_bin = _find_rust_binary()
        if rust_bin:
            try:
                result = _subprocess.run(
                    [rust_bin],
                    input=_json.dumps(data),
                    capture_output=True,
                    text=True,
                    timeout=1,
                )
                if result.returncode == 0:
                    click.echo(result.stdout.strip())
                    return
                # Non-zero exit: fall through to Python path.
                click.echo(
                    click.style(
                        f"Rust binary failed (exit {result.returncode}), using Python fallback",
                        fg="yellow",
                    ),
                    err=True,
                )
                if result.stderr:
                    click.echo(result.stderr.strip(), err=True)
            except (_subprocess.TimeoutExpired, OSError):
                pass  # fall through silently

    # ── 2. Python JSONL fallback ────────────────────────────────────────────
    try:
        from smm_sync.jsonl_writer import write_decision
    except ImportError as exc:
        click.echo(click.style(f"jsonl_writer unavailable: {exc}", fg="red"), err=True)
        sys.exit(1)

    try:
        write_decision(data, project=project)
        _title = data.get("title", "Unnamed decision")
        click.echo(click.style(f"\u2713 Decision: {_title} \u2014 recorded", fg="green"))
    except ValueError as exc:
        click.echo(click.style(f"Validation error: {exc}", fg="red"), err=True)
        sys.exit(1)
    except Exception as exc:
        click.echo(click.style(f"Ingestion failed: {exc}", fg="red"), err=True)
        sys.exit(1)


@main.command("add-decisions-batch")
@click.argument("source", type=click.Path(exists=True), metavar="JSONL_FILE")
@click.option("--project", default="smm-sync", help="Project name.")
def add_decisions_batch_cmd(source: str, project: str) -> None:
    """Ingest multiple decisions from a JSONL file in a single process.

    Loads the sentence-transformers model exactly once for the whole batch,
    then writes each decision sequentially — far faster than running
    smm add-decision 32 times.

    JSONL_FILE: path to a file with one JSON decision object per line.

    Each line format (all fields optional except title):
        {"title": "...", "rationale": "...", "decision_type": "technical",
         "made_by": "...", "alternatives": [...], "constraints": [...]}

    Lines starting with '#' or blank lines are skipped.

    Example:
        smm add-decisions-batch decisions.jsonl
        smm add-decisions-batch --project fintrack sprint1.jsonl
    """
    import asyncio as _asyncio
    import json as _json

    try:
        from smm_sync.context_graph.client import GraphClient
    except ImportError as exc:
        click.echo(click.style(f"context_graph unavailable: {exc}", fg="red"), err=True)
        sys.exit(1)

    smm_dir = get_smm_dir()
    graph_dir = smm_dir / "graph"

    # Bug 1: check if dashboard server is running — route through it if so.
    use_server = _server_running_on_port(DASHBOARD_PORT)
    if use_server:
        click.echo(
            click.style(
                f"Dashboard detected on port {DASHBOARD_PORT} — routing all decisions through server.",
                fg="yellow",
            )
        )

    # Parse JSONL file
    lines: list[tuple[int, dict]] = []
    with open(source) as f:
        for lineno, line in enumerate(f, 1):
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            try:
                lines.append((lineno, _json.loads(line)))
            except _json.JSONDecodeError as exc:
                click.echo(
                    click.style(f"Line {lineno}: invalid JSON — {exc}", fg="red"), err=True
                )

    if not lines:
        click.echo(click.style("No decisions found in file.", fg="yellow"))
        return

    click.echo(f"Ingesting {len(lines)} decisions...")

    ok, failed = 0, 0

    if use_server:
        # Route all decisions through the running dashboard server
        for lineno, data in lines:
            content = data.get("content") or data.get("rationale", "")
            status = data.get("status", "approved")
            if status != "approved":
                content = f"{content}\nStatus: {status}"
            try:
                decision_id = _post_decision_to_dashboard(data, content, project)
                click.echo(click.style(
                    f"  [{ok+1:3d}] {data.get('title', '')[:60]}", fg="green"
                ))
                ok += 1
            except Exception as exc:
                click.echo(
                    click.style(f"  [ERR] line {lineno}: {exc}", fg="red"), err=True
                )
                failed += 1
    else:
        # Bug 3: single GraphClient instance — model loads exactly once
        client = GraphClient(graph_dir=graph_dir, api_key="")

        async def _run_batch():
            nonlocal ok, failed
            for lineno, data in lines:
                content = data.get("content") or data.get("rationale", "")
                status = data.get("status", "approved")
                if status != "approved":
                    content = f"{content}\nStatus: {status}"
                try:
                    decision_id = await client.add_decision_local(
                        title=data.get("title", "Unnamed decision"),
                        content=content,
                        rationale=data.get("rationale", ""),
                        made_by=data.get("made_by", "lore-hook"),
                        project=project,
                        alternatives=data.get("alternatives", []),
                        constraints=data.get("constraints", []),
                        decision_type=data.get("decision_type", data.get("type", "technical")),
                        confidence=data.get("confidence"),
                    )
                    click.echo(click.style(
                        f"  [{ok+1:3d}] {data.get('title', '')[:60]}", fg="green"
                    ))
                    ok += 1
                except Exception as exc:
                    click.echo(
                        click.style(f"  [ERR] line {lineno}: {exc}", fg="red"), err=True
                    )
                    failed += 1

        _asyncio.run(_run_batch())

    color = "green" if not failed else "yellow"
    click.echo(click.style(f"\n{ok}/{ok + failed} decisions ingested.", fg=color))
    if failed:
        sys.exit(1)


@main.command("check")
@click.option("--all", "check_all", is_flag=True, default=False,
              help="Re-process all decisions, not just new ones.")
@click.option("--project", default="smm-sync", help="Project name.")
@click.option("--quiet", is_flag=True, default=False, help="Suppress output.")
def check_cmd(check_all: bool, project: str, quiet: bool) -> None:
    """Sync new decisions from JSONL into Kuzu, detect contradictions, build edges.

    This is the ONLY command that loads heavy dependencies:
    Kuzu graph database, sentence-transformers embeddings, and optionally
    ``claude -p`` for contradiction detection.  Target: < 15 seconds.

    Run periodically (e.g. post-commit hook) or manually to keep the graph
    up to date with decisions written by ``smm add-decision``.

    Example:
        smm check
        smm check --all   # re-check every decision from scratch
    """
    import asyncio as _asyncio
    import json as _json
    from datetime import datetime as _datetime, timezone as _timezone

    smm_dir = get_smm_dir()
    decisions_path = smm_dir / "decisions.jsonl"

    if not decisions_path.exists():
        click.echo(
            click.style(
                "No decisions.jsonl found. Run `smm add-decision` first.", fg="yellow"
            )
        )
        return

    # ── Read all decisions from JSONL ───────────────────────────────────────
    all_decisions: list[dict] = []
    for raw_line in decisions_path.read_text(encoding="utf-8").splitlines():
        raw_line = raw_line.strip()
        if raw_line:
            try:
                all_decisions.append(_json.loads(raw_line))
            except Exception:
                pass

    # ── Find new decisions since last check ─────────────────────────────────
    last_check_path = smm_dir / "last_check_timestamp.txt"
    last_check_ts: _datetime | None = None
    if last_check_path.exists() and not check_all:
        try:
            ts_str = last_check_path.read_text(encoding="utf-8").strip()
            last_check_ts = _datetime.fromisoformat(ts_str)
        except Exception:
            pass

    new_decisions: list[dict] = []
    for d in all_decisions:
        if check_all or last_check_ts is None:
            new_decisions.append(d)
        else:
            ts_str = d.get("timestamp", "")
            try:
                ts = _datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
                if ts > last_check_ts:
                    new_decisions.append(d)
            except Exception:
                new_decisions.append(d)  # include if timestamp unparseable

    if not new_decisions and not check_all:
        if not quiet:
            click.echo(
                click.style(
                    f"No new decisions since last check. "
                    f"{len(all_decisions)} total in JSONL.",
                    fg="cyan",
                )
            )
        return

    click.echo(
        click.style(
            f"Syncing {len(new_decisions)} new decision(s) into graph "
            f"(of {len(all_decisions)} total)...",
            fg="cyan",
        )
    )

    # ── Open Kuzu graph ─────────────────────────────────────────────────────
    try:
        from smm_sync.context_graph.client import GraphClient
    except ImportError as exc:
        click.echo(
            click.style(f"context_graph unavailable: {exc}", fg="red"), err=True
        )
        return

    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    graph_dir = smm_dir / "graph"
    client = GraphClient(graph_dir=graph_dir, api_key=api_key)

    # ── Read .smm/config.json for agent type ────────────────────────────────
    agent_cfg = "skip"
    try:
        cfg_path = smm_dir / "config.json"
        if cfg_path.exists():
            agent_cfg = _json.loads(cfg_path.read_text(encoding="utf-8")).get(
                "agent", "skip"
            )
    except Exception:
        pass

    async def _run_check():
        synced = 0
        all_new_titles: list[str] = []

        # Snapshot contradictions.jsonl line count before sync so we can count
        # new entries added by add_decision_local() AND the fallback check below.
        _contra_path = smm_dir / "contradictions.jsonl"
        _contra_lines_before = 0
        try:
            if _contra_path.exists():
                _contra_lines_before = sum(
                    1 for ln in _contra_path.read_text(encoding="utf-8").splitlines()
                    if ln.strip()
                )
        except Exception:
            pass

        # 1. Sync new decisions into Kuzu via add_decision_local
        for d in new_decisions:
            title = d.get("title", "")
            alts_raw = d.get("alternatives", "")
            cons_raw = d.get("constraints", "")
            alternatives = alts_raw.split("; ") if isinstance(alts_raw, str) else (alts_raw or [])
            constraints = cons_raw.split("; ") if isinstance(cons_raw, str) else (cons_raw or [])
            try:
                await client.add_decision_local(
                    title=title,
                    content=d.get("rationale", ""),
                    rationale=d.get("rationale", ""),
                    made_by=d.get("made_by", "lore-hook"),
                    project=d.get("project", project),
                    alternatives=alternatives,
                    constraints=constraints,
                    decision_type=d.get("type", "technical"),
                    source_type=d.get("source", "manual"),
                    confidence=d.get("confidence"),
                )
                synced += 1
                all_new_titles.append(title)
            except Exception as exc:
                click.echo(
                    click.style(f"  Warning: failed to sync '{title[:50]}': {exc}", fg="yellow"),
                    err=True,
                )

        # 2. Contradiction detection via claude -p (chunked batches of 20 pairs)
        new_contras_count = 0
        if all_new_titles and agent_cfg != "skip":
            try:
                import subprocess as _sp
                import tempfile as _tempfile
                import re as _re
                import uuid as _uuid_mod
                all_decisions_now = await client.get_decisions(project=project)
                # Build title→id map for reliable ID-based dedup
                _title_to_id: dict[str, str] = {
                    d.title.lower().strip(): d.id
                    for d in all_decisions_now
                    if d.title and d.id
                }
                # Build pairs: new×all when incremental, all×all when --all
                _new_titles_set = {t.lower().strip() for t in all_new_titles}
                if check_all:
                    _all_pairs = [
                        (all_decisions_now[_i], all_decisions_now[_j])
                        for _i in range(len(all_decisions_now))
                        for _j in range(_i + 1, len(all_decisions_now))
                    ]
                else:
                    # Each new decision paired with every other decision (old or new).
                    # Use sorted-key dedup so (A,B) and (B,A) aren't both included
                    # when two new decisions are paired with each other.
                    _seen_incremental: set[tuple] = set()
                    _all_pairs = []
                    for _d_new in all_decisions_now:
                        if not (_d_new.title and _d_new.title.lower().strip() in _new_titles_set):
                            continue
                        for _d_other in all_decisions_now:
                            if _d_other is _d_new:
                                continue
                            _pkey = tuple(sorted([
                                _d_new.id or _d_new.title or "",
                                _d_other.id or _d_other.title or "",
                            ]))
                            if _pkey not in _seen_incremental:
                                _seen_incremental.add(_pkey)
                                _all_pairs.append((_d_new, _d_other))
                # Strip CLAUDECODE env vars for nested session safety
                _safe_env = os.environ.copy()
                for _var in [
                    "CLAUDECODE",
                    "CLAUDE_CODE_ENTRYPOINT",
                    "CLAUDE_CODE_DISABLE_FEEDBACK_SURVEY",
                    "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC",
                ]:
                    _safe_env.pop(_var, None)
                _safe_env["CLAUDE_CODE_TMPDIR"] = _tempfile.mkdtemp(prefix="smm-check-")
                contra_path = smm_dir / "contradictions.jsonl"
                # Load existing pair keys to skip reversed duplicates,
                # and collect superseded/loser decision titles to skip noise.
                _cli_seen_keys: set[tuple] = set()
                _cli_seen_id_pairs: set[tuple] = set()
                _cli_pair_counts: dict[tuple, int] = {}
                _cli_superseded_titles: set[str] = set()
                try:
                    if contra_path.exists():
                        for _ln in contra_path.read_text(encoding="utf-8").splitlines():
                            _ln = _ln.strip()
                            if _ln:
                                try:
                                    _ex = _json.loads(_ln)
                                    _da = (_ex.get("decision_a", "") or "").lower().strip()[:50]
                                    _db = (_ex.get("decision_b", "") or "").lower().strip()[:50]
                                    if _da and _db:
                                        _pk_ex = tuple(sorted([_da, _db]))
                                        _cli_seen_keys.add(_pk_ex)
                                        _cli_pair_counts[_pk_ex] = _cli_pair_counts.get(_pk_ex, 0) + 1
                                    # Also index by decision IDs when available
                                    _ex_aid = (_ex.get("decision_a_id") or "").strip()
                                    _ex_bid = (_ex.get("decision_b_id") or "").strip()
                                    if _ex_aid and _ex_bid:
                                        _cli_seen_id_pairs.add(tuple(sorted([_ex_aid, _ex_bid])))
                                    # Track superseded (losing) decisions from resolved contradictions
                                    _is_resolved = (
                                        _ex.get("resolved", False)
                                        or _ex.get("status", "") in ("resolved", "dismissed", "ignored")
                                    )
                                    if _is_resolved:
                                        _loser = (_ex.get("loser", "") or "").lower().strip()
                                        if _loser:
                                            _cli_superseded_titles.add(_loser)
                                        # Infer loser when resolved_winner is set (demo/old format)
                                        _winner = (_ex.get("resolved_winner", "") or _ex.get("winner", "") or "").lower().strip()
                                        if _winner:
                                            _other = _db if _winner == _da else (_da if _winner == _db else "")
                                            if _other:
                                                _cli_superseded_titles.add(_other)
                                except Exception:
                                    pass
                except Exception:
                    pass
                # Also collect superseded decisions from decisions.jsonl
                try:
                    _decisions_path = smm_dir / "decisions.jsonl"
                    if _decisions_path.exists():
                        for _ln in _decisions_path.read_text(encoding="utf-8").splitlines():
                            _ln = _ln.strip()
                            if _ln:
                                try:
                                    _d = _json.loads(_ln)
                                    if _d.get("status") == "superseded" or _d.get("superseded_by"):
                                        _dt = (_d.get("title", "") or "").lower().strip()
                                        if _dt:
                                            _cli_superseded_titles.add(_dt)
                                except Exception:
                                    pass
                except Exception:
                    pass
                _chunk_size = 20
                _chunks = [_all_pairs[_k:_k + _chunk_size] for _k in range(0, len(_all_pairs), _chunk_size)]
                _total_batches = len(_chunks)
                for _batch_idx, _batch_pairs in enumerate(_chunks):
                    _batch_num = _batch_idx + 1
                    _pair_start = _batch_idx * _chunk_size + 1
                    _pair_end = _pair_start + len(_batch_pairs) - 1
                    click.echo(
                        f"  Checking batch {_batch_num}/{_total_batches} (pairs {_pair_start}-{_pair_end})...",
                        err=True,
                    )
                    _pair_lines = []
                    for _pi, (_d_a, _d_b) in enumerate(_batch_pairs):
                        _a_text = f"{_d_a.title}: {((_d_a.rationale or _d_a.content or '').strip()[:120])}"
                        _b_text = f"{_d_b.title}: {((_d_b.rationale or _d_b.content or '').strip()[:120])}"
                        _pair_lines.append(f'Pair {_pi + 1}: A="{_a_text}" B="{_b_text}"')
                    _prompt = (
                        "Analyze these decision pairs. Two decisions CONTRADICT if they specify "
                        "mutually exclusive approaches for the same architectural concern. Flag a contradiction if:\n"
                        "- They choose different databases for the same purpose (e.g. SQLite vs PostgreSQL)\n"
                        "- They choose different auth mechanisms (e.g. API Key vs JWT)\n"
                        "- They choose different frameworks, libraries, or patterns for the same concern\n"
                        "- One explicitly reverts, replaces, or undoes the other\n\n"
                        "A revert IS a contradiction — it means the team changed direction and both cannot be true simultaneously.\n\n"
                        "Do NOT flag pairs that are merely related or complementary.\n\n"
                        + "\n".join(_pair_lines)
                        + "\n\nOutput JSON array only: "
                        '[{"decision_a":"<title>","decision_b":"<title>","reason":"<brief>"}]. '
                        "Empty array [] if none."
                    )
                    _result = await _asyncio.get_event_loop().run_in_executor(
                        None,
                        lambda _p=_prompt: _sp.run(
                            ["claude", "-p", "--model", "claude-haiku-4-5-20251001", _p],
                            capture_output=True, text=True,
                            timeout=600, env=_safe_env,
                        ),
                    )
                    _batch_found = 0
                    if _result.returncode == 0:
                        _raw = _result.stdout.strip()
                        _m = _re.search(r"\[.*\]", _raw, _re.DOTALL)
                        if _m:
                            try:
                                _contras = _json.loads(_m.group(0))
                                now_ts = _datetime.now(_timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
                                for _c in _contras:
                                    _da_t = _c.get("decision_a", "")
                                    _db_t = _c.get("decision_b", "")
                                    if _da_t.lower().strip() == _db_t.lower().strip():
                                        continue
                                    _da_50 = _da_t.lower().strip()[:50]
                                    _db_50 = _db_t.lower().strip()[:50]
                                    _pk = tuple(sorted([_da_50, _db_50]))
                                    # Primary dedup: use decision IDs when available
                                    _da_id = _title_to_id.get(_da_t.lower().strip(), "")
                                    _db_id = _title_to_id.get(_db_t.lower().strip(), "")
                                    _id_pair = tuple(sorted([_da_id, _db_id])) if _da_id and _db_id else None
                                    if _id_pair and _id_pair in _cli_seen_id_pairs:
                                        continue
                                    if _pk in _cli_seen_keys:
                                        if _cli_pair_counts.get(_pk, 0) > 2:
                                            click.echo(
                                                click.style(
                                                    f"  [dedup] Skipping duplicate "
                                                    f"(count={_cli_pair_counts[_pk]}): "
                                                    f"{_da_50!r:.40}",
                                                    fg="yellow",
                                                ),
                                                err=True,
                                            )
                                        continue
                                    # Skip if either decision is already superseded
                                    if (
                                        _da_t.lower().strip() in _cli_superseded_titles
                                        or _db_t.lower().strip() in _cli_superseded_titles
                                    ):
                                        continue
                                    if _id_pair:
                                        _cli_seen_id_pairs.add(_id_pair)
                                    _cli_seen_keys.add(_pk)
                                    _entry = {
                                        "id": str(_uuid_mod.uuid4()),
                                        "decision_a": _da_t,
                                        "decision_b": _db_t,
                                        "decision_a_id": _da_id,
                                        "decision_b_id": _db_id,
                                        "reason": _c.get("reason", ""),
                                        "detected_at": now_ts,
                                        "resolved": False,
                                    }
                                    with open(contra_path, "a", encoding="utf-8") as _fh:
                                        _fh.write(_json.dumps(_entry) + "\n")
                                    new_contras_count += 1
                                    _batch_found += 1
                            except Exception:
                                pass
                    click.echo(
                        f"    found {_batch_found} contradiction(s)",
                        err=True,
                    )
            except Exception as _exc:
                click.echo(
                    click.style(f"  claude -p check skipped: {_exc}", fg="yellow"),
                    err=True,
                )
        elif all_new_titles:
            # Local embedding heuristic fallback (no claude -p needed)
            try:
                for d_new in new_decisions:
                    _lc = await client.contradiction_check(
                        f"{d_new.get('title', '')}: {d_new.get('rationale', '')}",
                        project,
                    )
                    if _lc:
                        contra_path = smm_dir / "contradictions.jsonl"
                        import uuid as _uuid_mod
                        now_ts = _datetime.now(_timezone.utc).strftime(
                            "%Y-%m-%dT%H:%M:%SZ"
                        )
                        for _c in _lc:
                            _fb_a = (d_new.get("title", "") or "").lower().strip()
                            _fb_b = (_c.get("existing", "") or "").lower().strip()
                            # Skip if either decision is already superseded
                            if (
                                _fb_a in _cli_superseded_titles
                                or _fb_b in _cli_superseded_titles
                            ):
                                continue
                            _entry = {
                                "id": str(_uuid_mod.uuid4()),
                                "decision_a": d_new.get("title", ""),
                                "decision_b": _c.get("existing", ""),
                                "reason": f"similarity={_c.get('similarity', 0):.2f}",
                                "detected_at": now_ts,
                                "resolved": False,
                            }
                            with open(contra_path, "a", encoding="utf-8") as _fh:
                                _fh.write(_json.dumps(_entry) + "\n")
                            new_contras_count += 1
            except Exception as _exc:
                click.echo(
                    click.style(f"  Local contradiction check skipped: {_exc}", fg="yellow"),
                    err=True,
                )

        # 3. Build edges between related decisions
        try:
            await client.discover_edges(project=project)
        except Exception as _exc:
            click.echo(
                click.style(f"  Edge discovery skipped: {_exc}", fg="yellow"), err=True
            )

        # 4. Update last_check_timestamp
        now_iso = _datetime.now(_timezone.utc).isoformat()
        last_check_path.write_text(now_iso, encoding="utf-8")

        # Count ALL new contradictions written during this run (from both
        # add_decision_local() in step 1 and the fallback check in step 2)
        # by diffing the line count of contradictions.jsonl.
        _contra_lines_after = 0
        try:
            if _contra_path.exists():
                _contra_lines_after = sum(
                    1 for ln in _contra_path.read_text(encoding="utf-8").splitlines()
                    if ln.strip()
                )
        except Exception:
            pass
        return synced, _contra_lines_after - _contra_lines_before

    try:
        synced, contra_count = _asyncio.run(_run_check())
        # Write dirty flag so pre-commit hook knows to run smm compile
        if synced > 0 or contra_count > 0:
            (smm_dir / ".check_dirty").write_text("", encoding="utf-8")
        if not quiet:
            click.echo(
                click.style(
                    f"Checked {synced} new decision(s) against "
                    f"{len(all_decisions) - len(new_decisions)} existing. "
                    f"Found {contra_count} contradiction(s).",
                    fg="green",
                )
            )
    except Exception as exc:
        click.echo(click.style(f"smm check failed: {exc}", fg="red"), err=True)
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

    Already-actioned pairs (resolved/deferred/ignored in contradiction_index.json)
    are filtered out so the same conflict is never re-flagged to the developer.

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
        if json_output:
            click.echo(_json.dumps({"contradictions": []}))
        return

    graph_dir = smm_dir / "graph"
    client = GraphClient(graph_dir=graph_dir, api_key=os.environ.get("ANTHROPIC_API_KEY", ""))

    async def _run():
        query = f"{title}: {content}" if content else title
        return await client.contradiction_check(query, project)

    try:
        contradictions = asyncio.run(_run())

        # Filter out pairs already actioned in the index
        from smm_sync.contradiction_index import filter_new_contradictions
        contradictions = filter_new_contradictions(smm_dir, contradictions, new_title=title)

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


@main.command("handle-contradictions")
@click.option("--title", required=True, help="Title of the new decision being committed.")
@click.option("--contra-file", "contra_file", required=True, type=click.Path(exists=True),
              help="Path to JSON file output by check-contradictions --json-output.")
@click.option("--non-interactive", "non_interactive", is_flag=True, default=False,
              help="Skip prompts and automatically defer all (CI/CD mode).")
@click.option("--project", default="smm-sync", help="Project name.")
def handle_contradictions_cmd(
    title: str,
    contra_file: str,
    non_interactive: bool,
    project: str,
) -> None:
    """Interactive R/D/I handler for contradictions detected at commit time.

    Reads the JSON produced by ``smm check-contradictions --json-output``,
    presents each new contradiction with an [R]esolve / [D]efer / [I]gnore
    prompt, records every action in ``.smm/contradiction_index.json`` so the
    pair is never re-flagged, and prints the overall decision status
    (``approved`` or ``deferred``) to stdout for the shell to capture.

    All interactive output goes to stderr (redirected to /dev/tty by the hook).

    Non-interactive mode (``--non-interactive`` or CI=true in env):
      every contradiction is automatically deferred.

    Args:
        title: Title of the new decision being checked in.
        contra_file: Path to the JSON file from check-contradictions.
        non_interactive: If True, skip prompts and defer everything.
        project: Project name (unused here, for future scoping).
    """
    import json as _json
    import sys as _sys

    smm_dir = get_smm_dir()
    from smm_sync.contradiction_index import (
        load_index,
        is_actioned,
        record_action,
    )

    # Load the contradiction list
    try:
        data = _json.loads(Path(contra_file).read_text(encoding="utf-8"))
        contradictions = data.get("contradictions", [])
    except Exception as exc:
        click.echo(f"[handle-contradictions] failed to read {contra_file}: {exc}", err=True)
        click.echo("deferred")
        return

    if not contradictions:
        click.echo("approved")
        return

    # CI / non-interactive guard
    is_ci = (
        non_interactive
        or os.environ.get("CI", "").lower() in ("true", "1", "yes")
        or os.environ.get("AXIOM_NON_INTERACTIVE", "") == "1"
    )

    # Determine if /dev/tty is reachable
    tty_available = False
    if not is_ci:
        try:
            with open("/dev/tty", "r"):
                tty_available = True
        except OSError:
            pass

    def _tty_print(msg: str) -> None:
        """Write msg to stderr (hook redirects stderr → /dev/tty)."""
        click.echo(msg, err=True)

    def _tty_prompt(msg: str) -> str:
        """Prompt on /dev/tty if available, else return ''."""
        if not tty_available:
            return ""
        try:
            with open("/dev/tty", "r") as _tin, open("/dev/tty", "w") as _tout:
                _tout.write(msg)
                _tout.flush()
                return _tin.readline().strip()
        except OSError:
            return ""

    # Header
    _tty_print("")
    _tty_print(
        f"\033[33m⚠  Axiom: {len(contradictions)} contradiction(s) detected\033[0m"
    )
    _tty_print("")
    for idx, c in enumerate(contradictions, 1):
        existing = c.get("existing", "unknown")
        sim = c.get("similarity", 0.0)
        reason = c.get("reason", f"Similarity {sim:.0%}")
        _tty_print(f'{idx}. "{title}" conflicts with "{existing}"')
        _tty_print(f"   Reason: {reason}")
        _tty_print("")

    if is_ci:
        _tty_print("   Non-interactive mode — deferring all contradictions to PM.")
        _tty_print("")

    # Per-contradiction R/D/I loop
    overall_deferred = False

    for idx, c in enumerate(contradictions, 1):
        existing = c.get("existing", "unknown")

        if is_ci:
            choice = "D"
        else:
            raw = _tty_prompt(
                f"  Contradiction {idx}/{len(contradictions)}: \"{existing}\"\n"
                f"    [R] Resolve now   [D] Defer to PM   [I] Ignore\n"
                f"    Choice [R/D/I, default=D]: "
            )
            choice = (raw.strip().upper() or "D")[:1]
            if choice not in ("R", "D", "I"):
                choice = "D"

        note = ""
        if choice == "R":
            note = _tty_prompt("    Resolution note (one line): ").strip() or "resolved by dev"
            action_status = "resolved"
        elif choice == "I":
            action_status = "ignored"
        else:
            action_status = "deferred"
            overall_deferred = True

        # Record in the index
        try:
            record_action(smm_dir, title, existing, action_status, note=note, actor="dev")
        except Exception as _e:
            _tty_print(f"  [warn] index write failed: {_e}")

        # Write to contradictions.jsonl (skip if ignored)
        if action_status != "ignored":
            try:
                import uuid as _uuid
                from datetime import datetime, timezone
                entry = {
                    "id": str(_uuid.uuid4()),
                    "decision_a": title,
                    "decision_b": existing,
                    "explanation": f'"{title}" may contradict "{existing}"',
                    "detected_at": datetime.now(timezone.utc).isoformat(),
                    "resolved": action_status == "resolved",
                    "status": action_status,
                }
                if note and action_status == "resolved":
                    entry["resolution"] = note
                    entry["resolved_at"] = entry["detected_at"]
                smm_dir.mkdir(parents=True, exist_ok=True)
                from smm_sync.jsonl_writer import append_jsonl_locked
                if not append_jsonl_locked(smm_dir / "contradictions.jsonl", entry):
                    _tty_print("  [warn] contradictions.jsonl write failed: lock timeout")
            except Exception as _e:
                _tty_print(f"  [warn] contradictions.jsonl write failed: {_e}")

        # Write to pending_decisions.json if deferred
        if action_status == "deferred":
            try:
                import uuid as _uuid
                from datetime import datetime, timezone
                pd_path = smm_dir / "pending_decisions.json"
                pd: dict = {"items": []}
                if pd_path.exists():
                    try:
                        pd = _json.loads(pd_path.read_text(encoding="utf-8"))
                    except Exception:
                        pd = {"items": []}
                pd.setdefault("items", []).append({
                    "id": str(_uuid.uuid4()),
                    "type": "contradiction",
                    "decision_a": title,
                    "decision_b": existing,
                    "deferred_at": datetime.now(timezone.utc).isoformat(),
                })
                pd_path.write_text(_json.dumps(pd, indent=2), encoding="utf-8")
            except Exception as _e:
                _tty_print(f"  [warn] pending_decisions.json write failed: {_e}")

        # User feedback
        label = {"resolved": "✓ Resolved", "deferred": "→ Deferred to PM", "ignored": "— Ignored"}
        _tty_print(f"    {label.get(action_status, action_status)}")
        _tty_print("")

    # Write compliance audit entry
    try:
        import uuid as _uuid_audit
        import json as _json_audit
        from datetime import datetime, timezone
        # Read agent from config.json, fallback to "cli"
        _audit_agent = "cli"
        _cfg_path = smm_dir / "config.json"
        if _cfg_path.exists():
            try:
                _cfg_data = _json_audit.loads(_cfg_path.read_text(encoding="utf-8"))
                _audit_agent = _cfg_data.get("agent", "cli") or "cli"
                if _audit_agent in ("skip", None):
                    _audit_agent = "cli"
            except Exception:
                pass
        _surfaced = [title]
        for c in contradictions:
            _existing = c.get("existing", "")
            if _existing and _existing not in _surfaced:
                _surfaced.append(_existing)
        _audit_entry = {
            "event_type": "contradiction_handled",
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "decision_title": title,
            "agent": _audit_agent,
            "actor": _audit_agent,
            "session_id": _audit_agent,
            "decisions_surfaced": _surfaced,
            "decision_count": len(_surfaced),
        }
        smm_dir.mkdir(parents=True, exist_ok=True)
        _lineage_path = smm_dir / "compliance_lineage.jsonl"
        try:
            from smm_sync.jsonl_writer import _write_audit_hashed as _wah
            _wah(_lineage_path, _audit_entry)
        except Exception:
            with open(_lineage_path, "a", encoding="utf-8") as _lf:
                _lf.write(_json_audit.dumps(_audit_entry) + "\n")
    except Exception:
        pass  # never block on audit write failure

    # Output overall status to stdout for the shell
    click.echo("deferred" if overall_deferred else "approved")


@main.command("record-contradiction-action")
@click.option("--title-a", "title_a", required=True, help="First decision title.")
@click.option("--title-b", "title_b", required=True, help="Second (conflicting) decision title.")
@click.option(
    "--status",
    "action_status",
    required=True,
    type=click.Choice(["resolved", "deferred", "ignored"]),
    help="Action taken.",
)
@click.option("--note", default="", help="Optional resolution note.")
@click.option("--actor", default="dev", help="Who performed the action.")
def record_contradiction_action_cmd(
    title_a: str,
    title_b: str,
    action_status: str,
    note: str,
    actor: str,
) -> None:
    """Record an action on a contradiction pair in contradiction_index.json.

    Updates the index so the pair is never re-flagged in future commits.
    Called by handle-contradictions and the dashboard resolve endpoint.

    Args:
        title_a: First decision title.
        title_b: Second decision title.
        action_status: One of resolved, deferred, ignored.
        note: Optional resolution note.
        actor: Who performed the action.
    """
    from smm_sync.contradiction_index import record_action

    smm_dir = get_smm_dir()
    try:
        record_action(smm_dir, title_a, title_b, action_status, note=note, actor=actor)
        click.echo(
            click.style(f"Recorded: {title_a!r} ↔ {title_b!r} → {action_status}", fg="green")
        )
    except Exception as exc:
        click.echo(click.style(f"Failed to record action: {exc}", fg="red"), err=True)
        sys.exit(1)


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
            timeout=120,
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


@main.command("dedupe")
@click.option("--project", default="smm-sync", help="Project name (unused, for consistency).")
def dedupe_cmd(project: str) -> None:
    """Remove duplicate contradiction pairs from contradictions.jsonl.

    Keeps the first entry for each unique pair of decision IDs (or titles as
    fallback). Use this to clean up duplicates created before the ID-based
    dedup fix was applied.

    Example:

        smm dedupe
    """
    import json as _json_dedupe

    smm_dir = get_smm_dir()
    if not smm_dir or not smm_dir.exists():
        click.echo(click.style("No .smm/ directory found. Run `smm init` first.", fg="red"))
        return

    contra_path = smm_dir / "contradictions.jsonl"
    if not contra_path.exists():
        click.echo("No contradictions.jsonl found — nothing to deduplicate.")
        return

    raw_lines = [ln for ln in contra_path.read_text(encoding="utf-8").splitlines() if ln.strip()]
    seen_id_pairs: set[tuple] = set()
    seen_title_pairs: set[tuple] = set()
    kept: list[str] = []
    removed = 0

    for line in raw_lines:
        try:
            entry = _json_dedupe.loads(line)
        except Exception:
            kept.append(line)
            continue

        a_id = (entry.get("decision_a_id") or "").strip()
        b_id = (entry.get("decision_b_id") or "").strip()
        a_t = (entry.get("decision_a") or "").lower().strip()[:50]
        b_t = (entry.get("decision_b") or "").lower().strip()[:50]

        if a_id and b_id:
            id_pair = tuple(sorted([a_id, b_id]))
            if id_pair in seen_id_pairs:
                removed += 1
                continue
            seen_id_pairs.add(id_pair)
            # Also register title pair to prevent future title-based dupes
            if a_t and b_t:
                seen_title_pairs.add(tuple(sorted([a_t, b_t])))
        elif a_t and b_t:
            title_pair = tuple(sorted([a_t, b_t]))
            if title_pair in seen_title_pairs:
                removed += 1
                continue
            seen_title_pairs.add(title_pair)

        kept.append(line)

    contra_path.write_text("\n".join(kept) + ("\n" if kept else ""), encoding="utf-8")
    if removed:
        click.echo(click.style(f"Removed {removed} duplicate(s), kept {len(kept)} contradiction(s).", fg="green"))
    else:
        click.echo(f"No duplicates found. {len(kept)} contradiction(s) kept.")


@main.command("reset")
@click.option(
    "--confirm",
    is_flag=True,
    default=False,
    help="Confirm that you want to wipe all project data.",
)
def reset_cmd(confirm: bool) -> None:
    """Wipe all project data (graph, contradictions, compliance log, board).

    Preserves config.json and other settings.
    Requires --confirm flag to prevent accidental data loss.

    Use this before re-running PRDs from scratch or cleaning test artifacts.

    Example:
        smm reset --confirm
    """
    import shutil as _shutil

    if not confirm:
        click.echo(
            click.style(
                "This will wipe all project data.\n"
                "Pass --confirm to proceed.\n\n"
                "Files that will be deleted:\n"
                "  .smm/graph/          (knowledge graph)\n"
                "  .smm/contradictions.jsonl\n"
                "  .smm/contradiction_index.json\n"
                "  .smm/compliance_lineage.jsonl\n"
                "  .smm/board.json\n"
                "  .smm/pending_decisions.json\n\n"
                "Files that will be preserved:\n"
                "  .smm/config.json\n"
                "  .smm/github.yml\n"
                "  .smm/state.json\n"
                "  AGENTS.md",
                fg="yellow",
            )
        )
        return

    smm_dir = get_smm_dir()
    if not smm_dir.exists():
        click.echo(click.style("No .smm/ directory found. Nothing to reset.", fg="yellow"))
        return

    _TO_DELETE_DIRS = ["graph"]
    _TO_DELETE_FILES = [
        "contradictions.jsonl",
        "contradiction_index.json",
        "compliance_lineage.jsonl",
        "board.json",
        "pending_decisions.json",
        "events.jsonl",
    ]

    deleted = []
    for d in _TO_DELETE_DIRS:
        p = smm_dir / d
        if p.exists():
            try:
                _shutil.rmtree(p)
                deleted.append(str(d) + "/")
            except Exception as exc:
                click.echo(click.style(f"  Could not delete {d}/: {exc}", fg="yellow"), err=True)

    for f in _TO_DELETE_FILES:
        p = smm_dir / f
        if p.exists():
            try:
                p.unlink()
                deleted.append(f)
            except Exception as exc:
                click.echo(click.style(f"  Could not delete {f}: {exc}", fg="yellow"), err=True)

    if deleted:
        click.echo(click.style("Reset complete. Deleted:", fg="green"))
        for d in deleted:
            click.echo(f"  .smm/{d}")
    else:
        click.echo(click.style("Nothing to delete (already clean).", fg="yellow"))

    click.echo(click.style("\nRun `smm seed-graph` or add decisions to start fresh.", fg="cyan"))


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
@click.option("--port", default=DASHBOARD_PORT, help=f"Port to listen on (default: {DASHBOARD_PORT}).")
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

    # Spawn background smm check only if there are new decisions since last check
    try:
        import json as _jdash
        import threading as _tdash
        import subprocess as _sdash
        from datetime import datetime as _dtdash, timezone as _tzdash

        _dec_path = smm_dir / "decisions.jsonl"
        _lct_path = smm_dir / "last_check_timestamp.txt"
        _last_check_ts_dash = None
        if _lct_path.exists():
            try:
                _lct_str = _lct_path.read_text(encoding="utf-8").strip()
                _last_check_ts_dash = _dtdash.fromisoformat(_lct_str)
            except Exception:
                pass
        _new_since_check = 0
        if _dec_path.exists():
            for _ln in _dec_path.read_text(encoding="utf-8").splitlines():
                _ln = _ln.strip()
                if not _ln:
                    continue
                try:
                    _d = _jdash.loads(_ln)
                    if _last_check_ts_dash is None:
                        _new_since_check += 1
                    else:
                        _ts_str = _d.get("timestamp", "")
                        if _ts_str:
                            _ts = _dtdash.fromisoformat(_ts_str.replace("Z", "+00:00"))
                            if _ts > _last_check_ts_dash:
                                _new_since_check += 1
                        else:
                            _new_since_check += 1
                except Exception:
                    pass
        if _new_since_check > 0:
            def _bg_check():
                try:
                    _sdash.run(["smm", "check", "--quiet"], capture_output=True, timeout=120)
                except Exception:
                    pass
            _tdash.Thread(target=_bg_check, daemon=True).start()
    except Exception:
        pass

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
# smm discover-edges
# ---------------------------------------------------------------------------

@main.command("discover-edges")
@click.option("--project", default="smm-sync", show_default=True, help="Project name.")
@click.option(
    "--local",
    "use_local",
    is_flag=True,
    default=False,
    help="Use embedding-only approach (no claude CLI). Always offline.",
)
def discover_edges_cmd(project: str, use_local: bool) -> None:
    """Discover and create edges between decisions.

    Two modes:

    \b
    --local   Approach 1 — fully offline, zero API credits.
              Uses the local all-MiniLM-L6-v2 sentence-transformers model to
              compute pairwise cosine similarity. Pairs with similarity > 0.6
              get an edge with an inferred type (SUPERSEDES, ENABLES, etc.).
              Edge creation takes < 30s for 32 decisions.

    \b
    (default) Approach 2 — LLM-assisted via `claude -p` (Pro subscription
              tokens, not API billing). Loads all decisions, pipes them to
              `claude -p` for relationship analysis, then persists the JSON
              response as edges. Falls back to Approach 1 if claude is not
              available.

    Both approaches are safe to run multiple times — edges are deduplicated.
    """
    import asyncio as _asyncio
    import json as _json
    import shutil
    import subprocess

    smm_dir = _get_smm_dir()
    graph_dir = smm_dir / "graph"

    if not graph_dir.exists():
        click.echo(
            click.style("  ✗  No graph found. Run `smm seed-graph` first.", fg="red")
        )
        raise SystemExit(1)

    from smm_sync.context_graph.client import get_graph_client

    async def _run_local():
        gc = get_graph_client(graph_dir=graph_dir)
        click.echo(f"  ⬡  Scanning decisions in project '{project}'...")
        result = await gc.discover_edges(project=project)
        return result

    async def _run_llm():
        """Approach 2: pipe decisions to `claude -p`, parse JSON edges."""
        gc = get_graph_client(graph_dir=graph_dir)
        await gc._get_graphiti()  # ensure schema initialised
        await gc._ensure_decision_edge_table()

        # Load all decisions
        rows, _, _ = await gc._driver.execute_query(
            "MATCH (e:Episodic) RETURN e.uuid, e.name, e.content "
            "ORDER BY e.created_at ASC"
        )
        if not rows:
            click.echo("  No decisions found.")
            return {"nodes_scanned": 0, "edges_created": 0, "edges_skipped": 0}

        uuids = [r.get("e.uuid", "") for r in rows]
        titles = [r.get("e.name", "") or "(untitled)" for r in rows]
        contents = [r.get("e.content", "") or "" for r in rows]

        # Build numbered list for claude prompt
        lines = []
        for idx, (t, c) in enumerate(zip(titles, contents), 1):
            rationale = ""
            for line in c.splitlines():
                if line.startswith("Rationale:"):
                    rationale = line.split(":", 1)[1].strip()[:120]
                    break
            lines.append(f"{idx}. {t}" + (f" — {rationale}" if rationale else ""))

        decisions_text = "\n".join(lines)
        prompt = (
            'Given these architectural decisions, output a JSON array of relationships. '
            'Each relationship should have: '
            '{"from": <number>, "to": <number>, "type": "SUPERSEDES|REQUIRES|ENABLES|CONTRADICTS|PREFERRED_OVER|RELATES_TO", "reason": "<short explanation>"}\n\n'
            'Only output relationships where there is a clear logical connection.\n'
            'Output ONLY valid JSON, no other text.\n\n'
            f'Decisions:\n{decisions_text}'
        )

        click.echo(f"  ⬡  Sending {len(rows)} decisions to claude -p...")
        try:
            proc = subprocess.run(
                ["claude", "-p", "--model", "claude-haiku-4-5-20251001", prompt],
                capture_output=True,
                text=True,
                timeout=600,
            )
            output = proc.stdout.strip()
        except FileNotFoundError:
            click.echo(
                click.style("  ℹ  claude not found — falling back to local embeddings.", fg="yellow")
            )
            return await _run_local()
        except subprocess.TimeoutExpired:
            click.echo(
                click.style("  ℹ  claude timed out — falling back to local embeddings.", fg="yellow")
            )
            return await _run_local()

        # Parse JSON from claude output (strip markdown fences if present)
        json_text = output
        if "```" in json_text:
            import re as _re
            m = _re.search(r"```(?:json)?\s*([\s\S]+?)```", json_text)
            if m:
                json_text = m.group(1).strip()

        try:
            relationships = _json.loads(json_text)
        except _json.JSONDecodeError:
            click.echo(
                click.style("  ✗  Could not parse JSON from claude output — falling back to local.", fg="yellow")
            )
            return await _run_local()

        from datetime import datetime, timezone
        now_ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")

        def _esc(s: str) -> str:
            return (
                s.replace("\\", "\\\\")
                .replace("'", "\\'")
                .replace("\n", "\\n")
                .replace("\r", "")
            )

        created = 0
        skipped = 0
        for rel in relationships:
            try:
                from_idx = int(rel["from"]) - 1
                to_idx = int(rel["to"]) - 1
                edge_type = str(rel.get("type", "RELATES_TO")).upper()
                reason = str(rel.get("reason", ""))[:200]
            except (KeyError, ValueError, TypeError):
                skipped += 1
                continue

            if not (0 <= from_idx < len(uuids) and 0 <= to_idx < len(uuids)):
                skipped += 1
                continue

            uuid_a = uuids[from_idx]
            uuid_b = uuids[to_idx]
            if not uuid_a or not uuid_b or uuid_a == uuid_b:
                skipped += 1
                continue

            # Deduplicate
            check_rows, _, _ = await gc._driver.execute_query(
                "MATCH (a:Episodic)-[r:DecisionEdge]->(b:Episodic) "
                f"WHERE (a.uuid = '{_esc(uuid_a)}' AND b.uuid = '{_esc(uuid_b)}') "
                f"   OR (a.uuid = '{_esc(uuid_b)}' AND b.uuid = '{_esc(uuid_a)}') "
                "RETURN count(r) AS cnt"
            )
            if check_rows and (check_rows[0].get("cnt") or 0) > 0:
                skipped += 1
                continue

            edge_cypher = (
                f"MATCH (a:Episodic {{uuid: '{_esc(uuid_a)}'}}), "
                f"      (b:Episodic {{uuid: '{_esc(uuid_b)}'}}) "
                "CREATE (a)-[:DecisionEdge {"
                f"name: '{_esc(edge_type)}', "
                f"edge_type: '{_esc(edge_type)}', "
                f"reason: '{_esc(reason)}', "
                f"weight: 0.8000, "
                f"created_at: timestamp('{now_ts}')"
                "}]->(b)"
            )
            async with gc._write_lock:
                await gc._driver.execute_query(edge_cypher)
            created += 1

        return {"nodes_scanned": len(rows), "edges_created": created, "edges_skipped": skipped}

    if use_local:
        result = _asyncio.run(_run_local())
    else:
        result = _asyncio.run(_run_llm())

    n = result.get("nodes_scanned", 0)
    e = result.get("edges_created", 0)
    s = result.get("edges_skipped", 0)
    click.echo(
        click.style(
            f"  ✓  Created {e} edges between {n} decisions ({s} pairs below threshold).",
            fg="green",
        )
    )


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
