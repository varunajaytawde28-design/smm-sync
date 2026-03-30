# CLI Reference

All commands are invoked as `smm <command>`. Back to [README](../README.md).

**v1 scope:** Core commands — `smm init`, `smm serve`, `smm check`, `smm add-decision`, `smm dashboard`, and `smm dedupe` — are fully tested. Advanced commands for GitHub capture, natural language queries, digest generation, and graph seeding are functional but not yet validated for production use. They ship for early adopters; validation planned for v1.1.

---

## `smm init`

Scaffold AGENTS.md, `.smm/` directory, `.claude/settings.json` hooks, and agent-specific config files.

| Flag | Type | Default | Description |
|------|------|---------|-------------|
| `--name` | `str` | directory name | Project name |
| `--mode` | `dev\|dashboard` | `dev` | `dev` = MCP only; `dashboard` = also launches web UI at `http://localhost:7842` |

Prompts for agent type: `claude-code`, `cursor`, `both`, or `skip`.

---

## `smm serve`

Start the MCP server (stdio transport).

| Flag | Type | Default | Description |
|------|------|---------|-------------|
| `--host` | `str` | `127.0.0.1` | Host to bind to |
| `--port` | `int` | `0` | Port (0 = auto-assign) |

---

## `smm check`

Sync new decisions from JSONL into Kuzu, detect contradictions, build edges. This is the only command that loads heavy dependencies (Kuzu, sentence-transformers, optionally Claude).

| Flag | Type | Default | Description |
|------|------|---------|-------------|
| `--all` | flag | `False` | Re-process all decisions, not just new ones |
| `--project` | `str` | `smm-sync` | Project name |
| `--quiet` | flag | `False` | Suppress output |

**Contradiction detection model:** Currently hardcoded to Claude Haiku for speed. You can change this to Sonnet or any other model in `cli.py` — expect better detection accuracy but longer check times. If you're not using Claude, swap in any LLM that accepts the Anthropic SDK interface or modify the detection prompt for your preferred provider.

---

## `smm add-decision`

Record a decision from JSON (stdin or file) or named flags. Hot path: tries the compiled Rust binary (`smm-fast-write`) first (~10 ms), falls back to pure-Python JSONL append (< 500 ms). Neither path loads Kuzu, embeddings, or an LLM.

| Flag | Type | Default | Description |
|------|------|---------|-------------|
| `[JSON_FILE\|-]` | positional | stdin | JSON source |
| `--project` | `str` | `smm-sync` | Project name |
| `--title` | `str` | — | Decision title (alternative to JSON input) |
| `--description` | `str` | — | Decision description/rationale |
| `--type` | `str` | — | `architectural` / `technical` / `product` / `constraint` |
| `--confidence` | `float` | — | Confidence score (0.0–1.0) |
| `--made-by` | `str` | — | Who made this decision |
| `--context` | `str` | — | Context note: PRD name, ticket ID, source description |
| `--local` | flag | `False` | Kept for backward compatibility |

JSON fields: `title` (required), `rationale` (required), `type` (required), `confidence`, `alternatives` (list), `constraints` (list), `made_by`, `source`.

---

## `smm add-decisions-batch`

Ingest multiple decisions from a JSONL file in a single process. Loads sentence-transformers once for the whole batch.

| Flag | Type | Default | Description |
|------|------|---------|-------------|
| `JSONL_FILE` | positional | required | Path to JSONL file |
| `--project` | `str` | `smm-sync` | Project name |

---

## `smm check-contradictions`

Check if a decision contradicts existing ones. Used by the Axiom Lore-Hook before commit. Already-actioned pairs (resolved/deferred/ignored in `contradiction_index.json`) are filtered out.

| Flag | Type | Default | Description |
|------|------|---------|-------------|
| `--title` | `str` | required | Decision title to check |
| `--content` | `str` | `""` | Decision content/rationale |
| `--project` | `str` | `smm-sync` | Project name |
| `--json-output` | flag | `False` | Output as JSON for scripting |

---

## `smm handle-contradictions`

Interactive Resolve/Defer/Ignore handler for contradictions detected at commit time. Reads the JSON produced by `smm check-contradictions --json-output`, presents each conflict, records actions in `.smm/contradiction_index.json`, and prints `approved` or `deferred` to stdout for the shell to capture.

| Flag | Type | Default | Description |
|------|------|---------|-------------|
| `--title` | `str` | required | Title of the new decision being committed |
| `--contra-file` | path | required | Path to JSON output by `check-contradictions --json-output` |
| `--non-interactive` | flag | `False` | Auto-defer all (CI/CD mode) |
| `--project` | `str` | `smm-sync` | Project name |

Non-interactive mode is also triggered by `CI=true` or `AXIOM_NON_INTERACTIVE=1` in the environment.

---

## `smm record-contradiction-action`

Record an action on a contradiction pair so it is never re-flagged.

| Flag | Type | Default | Description |
|------|------|---------|-------------|
| `--title-a` | `str` | required | First decision title |
| `--title-b` | `str` | required | Second decision title |
| `--status` | `resolved\|deferred\|ignored` | required | Action taken |
| `--note` | `str` | `""` | Resolution note |
| `--actor` | `str` | `dev` | Who performed the action |

---

## `smm get-context`

Output a clean summary of project decisions, contradictions, and PM resolutions. Reads directly from JSONL files — no model loading, no graph sync. Readable by AI agents and humans.

| Flag | Type | Default | Description |
|------|------|---------|-------------|
| `--project` | `str` | `smm-sync` | Project name |

---

## `smm dashboard`

Start the CaaS Dashboard web UI. Opens `http://localhost:7842` in your browser.

| Flag | Type | Default | Description |
|------|------|---------|-------------|
| `--host` | `str` | `127.0.0.1` | Host to bind to |
| `--port` | `int` | `7842` | Port to listen on |

---

## `smm digest` *(advanced — not yet validated)*

Print a digest of CaaS activity for a period. Zero LLM calls. Shows decisions captured, architecture alerts, agent activity, estimated time saved, and graph health.

| Flag | Type | Default | Description |
|------|------|---------|-------------|
| `--period` | `day\|week\|month` | `week` | Time period |
| `--slack-webhook` | `str` | env `CAAS_SLACK_WEBHOOK` | Slack webhook URL |
| `--json` | flag | `False` | Output as JSON |

Schedule weekly via crontab: `0 9 * * 1 cd /your/project && smm digest --slack-webhook $CAAS_SLACK_WEBHOOK`

---

## `smm dedupe`

Remove duplicate contradiction pairs from `contradictions.jsonl`. Keeps the first entry for each unique pair of decision IDs.

| Flag | Type | Default | Description |
|------|------|---------|-------------|
| `--project` | `str` | `smm-sync` | Project name (unused, for consistency) |

---

## `smm onboard` *(advanced — not yet validated)*

Generate an AI-powered `ONBOARDING.md` from the context graph using Claude Haiku (~$0.003 per run). Commit the output — it's meant to be shared with the team.

| Flag | Type | Default | Description |
|------|------|---------|-------------|
| `--output` | `str` | `ONBOARDING.md` | Output file path |
| `--project` | `str` | inferred | Project name |

---

## `smm discover-edges` *(advanced — not yet validated)*

Discover and create edges between decisions in the graph. Two modes: `--local` (fully offline, cosine similarity > 0.6) or default (LLM-assisted via `claude -p`, falls back to local). Safe to run multiple times — edges are deduplicated.

| Flag | Type | Default | Description |
|------|------|---------|-------------|
| `--project` | `str` | `smm-sync` | Project name |
| `--local` | flag | `False` | Embedding-only (no Claude CLI). Fully offline. |

---

## `smm seed-graph` *(advanced — not yet validated)*

Seed the context graph with 18 interconnected architectural decisions. Makes ~54–126 Anthropic API calls, takes 5–10 minutes. Run once — the graph persists at `.smm/graph/`.

| Flag | Type | Default | Description |
|------|------|---------|-------------|
| `--project` | `str` | `smm-sync` | Project name |

Requires `ANTHROPIC_API_KEY` to be set.

---

## `smm query` *(advanced — not yet validated)*

Query the context graph with a natural language question.

| Flag | Type | Default | Description |
|------|------|---------|-------------|
| `QUESTION` | positional | required | Natural language question |
| `--project` | `str` | `smm-sync` | Project name |
| `--limit` | `int` | `5` | Max results |

Example: `smm query "why did we reject LWW CRDT?"`

---

## `smm decisions`

List all recorded decisions for a project.

| Flag | Type | Default | Description |
|------|------|---------|-------------|
| `--project` | `str` | `smm-sync` | Project name |

---

## `smm sync-from-git` *(advanced — not yet validated)*

Parse `Axiom-*` git trailers from commit history and ingest decisions. The "new team member" path: after cloning a repo with Axiom trailers, run this to populate the local graph.

| Flag | Type | Default | Description |
|------|------|---------|-------------|
| `--project` | `str` | `smm-sync` | Project name |
| `--dry-run` | flag | `False` | Show decisions without ingesting |

Reads: `Axiom-Decision`, `Axiom-Rationale`, `Axiom-Type`, `Axiom-Status` trailers from `git log`.

---

## `smm status`

Show current coordination state: claimed files and active sessions. No flags.

---

## `smm claim` *(advanced — not yet validated)*

Atomically claim a file using Tuple Space + event log.

| Flag | Type | Default | Description |
|------|------|---------|-------------|
| `FILEPATH` | positional | required | Path to claim |
| `--session` | `str` | hostname:pid | Session identifier |
| `--task` | `str` | `""` | Description of the task |

---

## `smm release` *(advanced — not yet validated)*

Release a claimed file.

| Flag | Type | Default | Description |
|------|------|---------|-------------|
| `FILEPATH` | positional | required | Path to release |
| `--session` | `str` | `""` | Session identifier |

---

## `smm refresh`

Read `AGENTS.md` and update `.smm/parsed_context.json`.

| Flag | Type | Default | Description |
|------|------|---------|-------------|
| `--quiet` | flag | `False` | Suppress output |

---

## `smm reset`

Wipe all project data (graph, contradictions, compliance log, board). Preserves `config.json` and settings.

| Flag | Type | Default | Description |
|------|------|---------|-------------|
| `--confirm` | flag | required | Confirm data wipe |

---

## `smm install` *(advanced — not yet validated)*

Interactive setup wizard. One command that does everything:

1. Prompts for Anthropic API key (validates it)
2. Prompts for GitHub token (validates it)
3. Detects git remote automatically
4. Creates `.smm/` directory and config files
5. Runs first GitHub capture
6. Seeds knowledge graph
7. Configures `.mcp.json` for Claude Code
8. Prints next steps

Keys are stored in `.smm/.env` (gitignored). Never written to any tracked file.

---

## `smm setup` *(advanced — not yet validated)*

Interactive wizard to onboard a new repository to CaaS. All steps are idempotent — safe to re-run.

| Flag | Type | Default | Description |
|------|------|---------|-------------|
| `--project` | `str` | inferred | Project name |
| `--skip-capture` | flag | `False` | Skip initial GitHub capture |
| `--skip-onboarding` | flag | `False` | Skip ONBOARDING.md generation |

---

## `smm capture init` *(advanced — not yet validated)*

Create `.smm/github.yml` with repos pre-configured from git remote. No flags.

## `smm capture run` *(advanced — not yet validated)*

Run the GitHub capture pipeline. Requires `GITHUB_TOKEN` and `ANTHROPIC_API_KEY`.

| Flag | Type | Default | Description |
|------|------|---------|-------------|
| `--once` | flag | `False` | Run once and exit (default: run forever) |
| `--since` | `str` | — | Backfill from date (YYYY-MM-DD) |

## `smm capture status` *(advanced — not yet validated)*

Show current capture state. No flags.

---

## `smm compliance show` *(advanced — not yet validated)*

Show the compliance audit trail.

| Flag | Type | Default | Description |
|------|------|---------|-------------|
| `--session` | `str` | `""` | Filter by session ID |
| `--decision` | `str` | `""` | Filter by decision title |

## `smm compliance stats` *(advanced — not yet validated)*

Show compliance lineage summary statistics. No flags.

---

## Configuration

### Environment Variables

| Variable | Required | Description |
|----------|----------|-------------|
| `ANTHROPIC_API_KEY` | For graph sync, capture, onboarding | Anthropic API key for Claude calls |
| `GITHUB_TOKEN` | For GitHub capture | GitHub personal access token |
| `CAAS_SLACK_WEBHOOK` | Optional | Slack webhook for `smm digest` |
| `SMM_DASHBOARD_PORT` | Optional | Override dashboard port (default: 7842) |
| `SMM_FAST_WRITE_BIN` | Optional | Path to compiled Rust binary override |
| `CAAS_DEBUG` | Optional | Enable debug logging in security module |
| `AXIOM_NON_INTERACTIVE` | Optional | Set to `1` for non-interactive contradiction handling |

### `.smm/` Directory Contents

```
.smm/
├── config.json                 # Agent type, project settings
├── decisions.jsonl             # All recorded decisions (primary source of truth)
├── contradictions.jsonl        # Detected contradictions with resolution status
├── contradiction_index.json    # Actioned pairs (never re-flagged)
├── compliance_lineage.jsonl    # SHA-256 hash-chained audit trail
├── board.json                  # Kanban board items
├── parsed_context.json         # Cached AGENTS.md parse output
├── events.jsonl                # Propose-validate-commit event log
├── state.json                  # Materialized coordination state
├── killed_sessions.json        # Sessions disconnected via dashboard
├── .check_dirty                # Flag for pre-commit hook
├── graph/                      # Kuzu embedded graph database
├── locks/                      # Atomic file claim locks
├── capture_state.json          # GitHub capture watermarks
└── github.yml                  # GitHub capture repo config
```
