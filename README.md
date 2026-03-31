# Axiom Hub

**Persistent decision memory and contradiction prevention for AI coding agents.**

<!-- Badges -->
[![PyPI version](https://img.shields.io/pypi/v/smm-sync.svg)](https://pypi.org/project/smm-sync/)
[![License](https://img.shields.io/badge/license-MIT-blue.svg)](#license)
[![Python](https://img.shields.io/badge/python-3.11%2B-blue.svg)](https://www.python.org/)

---

## The Problem

AI coding agents make dozens of architectural decisions per session — which database to use, how to structure an API, whether to split a module. When the session ends, those decisions vanish. The next session starts from zero, re-discovers the same trade-offs, and often reaches a different conclusion. After a few weeks, the codebase is a layer cake of contradictory choices that no one can explain.

This is the **Week Seven Wall**. The first few weeks of AI-assisted development feel magical. Then contradictions accumulate silently — a module uses SQLite because Session 12 decided it was simpler, while another module uses Postgres because Session 19 decided it was necessary. Neither session knew about the other's reasoning. The human developer becomes a full-time archaeologist, reverse-engineering why things are the way they are.

This problem became personal building with Claude Code. You give it a PRD, start building together, and the code flows — working features without getting tired. But across sessions, the LLM quietly drifts from the original concept. Small deviations compound. As a developer and PM, I realized I had no visibility into what the agent decided or why. I was clicking enter every 10 minutes and losing control of my own architecture. Axiom Hub exists because the people steering the product — developers and PMs — deserve to understand and govern the code being written on their behalf.

Axiom Hub fixes this by giving every AI session a shared memory of what was decided, why, and what was rejected. Contradictions are detected automatically and surfaced before they become bugs. Every decision gets an audit trail. The agent can't even start working until it loads context from prior sessions.

---

## How It Works

```
  ┌─────────────────────────────────────────────────────────────────┐
  │                        AI AGENT SESSION                         │
  │                                                                 │
  │  1. Agent starts ──► get_project_context()                      │
  │     ┌──────────────────────────────────────────────┐            │
  │     │ Returns: active decisions, unresolved         │            │
  │     │ contradictions, constraints, session token    │            │
  │     └──────────────────────────────────────────────┘            │
  │                          │                                      │
  │  2. Agent works ──► add_decision() (before writing code)        │
  │     Records: title, rationale, alternatives, constraints        │
  │                          │                                      │
  │  3. Agent finishes ──► complete_session(token)                  │
  │     ┌──────────────────────────────────────────────┐            │
  │     │ Reviews decisions captured during coding,      │            │
  │     │ lists modified files for missed decisions,     │            │
  │     │ fires background smm check                    │            │
  │     └──────────────────────────────────────────────┘            │
  │                          │                                      │
  └──────────────────────────┼──────────────────────────────────────┘
                             │
                    ┌────────▼────────┐
                    │   smm check     │  (background / post-commit)
                    │                 │
                    │  • Sync JSONL   │
                    │    into Kuzu    │
                    │  • Detect       │
                    │    contradictions│
                    │  • Build edges  │
                    └────────┬────────┘
                             │
                    ┌────────▼────────┐
                    │  NEXT SESSION   │
                    │                 │
                    │  get_project_   │
                    │  context()      │
                    │  shows new      │
                    │  contradictions │
                    │  + all prior    │
                    │  decisions      │
                    └─────────────────┘
```

---

## Quickstart

```bash
# Install
pip install git+https://github.com/varunajaytawde28-design/smm-sync.git

# Initialize in your project root
cd your-project
smm init

# Add the MCP server to your agent config (.mcp.json):
cat <<'EOF' > .mcp.json
{
  "mcpServers": {
    "smm-sync": {
      "command": "smm",
      "args": ["serve"]
    }
  }
}
EOF

# Start coding — the agent will call get_project_context automatically
```

---

### See It Work

**PRDs 1–3:** Team builds a monitoring service

Agent captures decisions across sessions: SQLite for simplicity,
FastAPI with BackgroundTasks, API key auth, single-process deployment

**PRD 4:** Add scheduled pings via Celery workers

> "Add Celery Beat to ping all targets every 60 seconds"

Agent captures: "Celery with 3 workers for parallel pings"
Reasonable — Celery is the standard for scheduled tasks

**PRD 5:** Add alert deduplication

> "Before creating an alert, check if one already exists"

Agent writes: `SELECT` then `INSERT` into SQLite from the Celery worker

Axiom Hub flags:

```
⚠️ "SQLite single-file database" ↔ "Celery with 3 workers"
   SQLite has a single-writer lock. Three Celery workers
   doing concurrent SELECT+INSERT will deadlock under load.
```

Developer resolves → switches alerts to async queue writes

No CLAUDE.md rule could have predicted this. "Use SQLite" was right when the app was single-process. "Use Celery workers" was right when the app needed scheduling. The conflict only exists because of decisions made in different sessions for different reasons — and that's exactly what Axiom Hub catches.

---

## Core Commands

| Command | What it does |
|---------|-------------|
| `smm init` | Scaffold `.smm/`, AGENTS.md, hooks, and agent config files |
| `smm add-decision` | Record a decision (< 1s, JSONL append only, no heavy deps) |
| `smm check` | Detect contradictions — new decisions vs all existing. `--all` for full re-scan |
| `smm dashboard` | Launch the web UI at `http://localhost:7842` |

**In practice, you only run `smm dashboard`.** Once Axiom Hub is initialized and added to your project, the AI agent handles everything else automatically — loading context, capturing decisions, completing sessions, and triggering contradiction checks. The CLI commands exist for manual overrides and debugging.

Additional commands documented in [docs/cli.md](docs/cli.md).

---

## MCP Tools

When an agent connects via MCP (`smm serve`), these tools are available:

| Tool | What it does |
|------|-------------|
| `get_project_context` | **Call first.** Returns decisions, contradictions, constraints, session token |
| `add_decision` | Record a decision (title, rationale, alternatives, type, confidence) |
| `complete_session` | Close session, summarize captures, trigger background check |
| `resolve_contradiction` | Pick winner A/B or dismiss — requires developer typing `YES`. Note: agent may attempt to auto-resolve; the confirmation gate blocks it but the agent sometimes skips rather than retrying. Enforcement improvement planned for v1.1. |

Additional tools like `query_decisions` (natural language search with Deja Vu detection) and `get_path_context` (JIT file-level constraints) exist in code but are not yet validated for production use.

**Resolution feedback loop:** When a contradiction is resolved on the dashboard, the winning decision stays active and the loser is marked superseded. The next session's `get_project_context` returns only winning decisions — the agent automatically builds on the correct architecture going forward. Note: existing code built on the losing decision is not auto-refactored; that requires a follow-up task.

17 tools total. See **[docs/mcp-tools.md](docs/mcp-tools.md)** for the full reference with all parameters.

---

## Two-State Session Machine

Axiom Hub enforces a session discipline through a lock file at `/tmp/smm-session-<hash>.lock`. Until the agent calls `get_project_context`, the PreToolUse hook blocks every tool call. The agent literally cannot read files, write code, or run commands without loading prior decisions first.

Once context is loaded, the lock file has a 30-minute TTL. The TTL exists as a safety net — if a developer accidentally closes their Claude Code session, the lock auto-expires so the next session isn't permanently blocked. When the agent runs `git commit`, a PostToolUse hook deletes the lock and prints *"Session complete. Next task will require fresh context load."* A second PostToolUse hook fires after every file write, reminding the agent to call `add_decision` for any architectural choice it just made.

At commit time, the Lore-Hook classifies the diff for architectural decisions (currently hardcoded to Claude Haiku; configurable model support planned for v1.1), runs contradiction checks against the graph, and presents interactive Resolve/Defer/Ignore prompts. Git trailers (`Axiom-Decision`, `Axiom-Rationale`, `Axiom-Type`) are injected into the commit message, and a background `smm check` fires for the next session.

The file locking mechanism was designed with multi-agent collaboration in mind. The locking model is designed to support safer multi-agent workflows by forcing fresh context loads and reducing conflicting work on shared code paths.

---

## Dashboard

Start with `smm dashboard` (default: `http://localhost:7842`).

- **Overview** (`/`) — Health cards, today's captures with approve/reject, contradiction A/B diff view, agent status, Cmd+K command palette
- **All Decisions** (`/decisions`) — Searchable, filterable table with type tags and confidence pills. Export as CSV, PDF, or ADR markdown
- **Decision Graph** (`/graph`) — Interactive Cytoscape.js DAG with category swim lanes, SUPERSEDES/CONTRADICTS/RELATES_TO edges, search and click-to-detail
- **Decision Board** (`/board`) — Kanban view (Backlog / Done) for contradiction resolution
- **Audit Trail** (`/compliance`) — SHA-256 hash-chained compliance log (work in progress — foundation laid for EU AI Act Article 12 and SOC 2)

**Note:** Contradictions detected in background update the dashboard in real time while it's open.

---

## Agent Compatibility

`smm init` generates the appropriate config files for each agent:

| Agent | Config Generated | How Context Is Loaded |
|-------|------------------|-----------------------|
| **Claude Code** | `.claude/settings.json` (PreToolUse + PostToolUse hooks) | MCP tools via `.mcp.json` |
| **Cursor** | `.cursor/rules/axiom-hub.mdc` (alwaysApply rule) | MCP tools via `.mcp.json` |
| **Windsurf** | `.agents/skills/axiom-caas/SKILL.md` | agentskills.io standard |
| **Cline** | `.agents/skills/axiom-caas/SKILL.md` | agentskills.io standard |
| **Copilot** | `AGENTS.md` (auto-read) | AGENTS.md conventions |
| **Devin** | `AGENTS.md` (auto-read) | AGENTS.md conventions |
| **Codex** | `AGENTS.md` (auto-read) | AGENTS.md conventions |

**Tested with Claude Code.** Config files for Cursor, Windsurf, Cline, Copilot, Devin, and Codex are generated by `smm init` but have not been validated end-to-end. Community testing welcome.

---

## Architecture

```
src/smm_sync/
├── cli.py                     # Click CLI entry point (30+ commands)
├── mcp_server.py              # FastMCP server (17 tools, session machine)
├── config.py                  # Project root detection, smm.toml parser
├── ingester.py                # AGENTS.md parser → parsed_context.json
├── jsonl_writer.py            # Pure-Python JSONL writer (hot path < 500 ms)
├── contradiction_index.py     # Actioned pair tracking (never re-flag)
├── lore_hook.py               # Git hook templates: diff classification, trailers
├── git_utils.py               # Pre-commit hook install, git remote parsing
├── context_graph/
│   ├── client.py              # Kuzu + Graphiti graph client, contradiction detection
│   ├── models.py              # Decision, ContextResult, RejectionResult
│   └── seed.py                # 18 interconnected seed decisions
├── capture/
│   ├── github_capture.py      # GitHub passive capture (Haiku + Sonnet pipeline)
│   └── models.py              # RepoConfig, CaptureSettings, CapturedEvent
├── compliance/
│   └── lineage.py             # Append-only audit logger (EU AI Act, SOC 2)
└── dashboard/
    ├── app.py                 # FastAPI backend + REST API
    └── static/                # 9 HTML pages (overview, graph, board, etc.)
```

---

## Known Limitations

- **LLM judgment varies.** Contradiction detection uses Claude Haiku as the primary judge with sentence-transformer embeddings as pre-filter. Catches obvious conflicts reliably; may miss nuanced reversions. Run `smm check --all` for full re-scan.
- **Agent may not wait for resolution.** The agent presents contradictions but may proceed without waiting for your answer. Say "stop, resolve first" to enforce. `isError` gate planned for v1.1.
- **Kuzu single-writer lock.** Don't run `smm check` and `smm dashboard` simultaneously.
- **Stale lock files.** Kill Claude Code mid-session → stale lock persists. Auto-cleaned on server startup and after 30-min TTL.
- **First run downloads ~80MB model** from HuggingFace. Cached after first download.
- **JSONL is append-only.** Files grow over time. Use `smm dedupe` to clean duplicates.

---

## Roadmap

### v1.1

- **`isError` gate** — Contradiction resolution flow returns `isError: true` to the MCP client so the agent must address conflicts before proceeding
- **Rust binary for `add-decision`** — Compiled via maturin, target < 10 ms writes (currently behind `smm-fast-write` stub)
- **Kuzu lock retry** — Retry with backoff when the graph database is locked by another process
- **NLI pre-filter** — Natural Language Inference model to pre-filter contradiction candidates before embedding similarity
- **`--model` flag** — Override the default Claude model for contradiction detection and capture
- **Smart incremental checking** — Only re-check decisions affected by new additions, not the full graph
- **OAuth/RBAC on MCP tools** — Authentication and role-based access control for multi-user MCP server deployments

---

## Contributing

```bash
git clone https://github.com/varunajaytawde28-design/smm-sync.git
cd smm-sync
pip install -e ".[dev]"

pytest tests/                    # all tests
pytest tests/ -m "not slow"      # skip API-calling tests
```

---

## License

MIT
