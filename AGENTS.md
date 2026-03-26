# AGENTS.md — smm-sync

> Source of truth for this project. Edit this file directly.
> Run `smm refresh` after editing.

## Project

smm-sync: Coordination layer for simultaneous AI agents working on the same codebase. Automatically parses shared context from AGENTS.md and coordinates file ownership using Tuple Space protocol + propose-validate-commit event log.

**Stack:** python 3.11+, click, jinja2, filelock, mcp

## Architecture

### AGENTS.md is the source of truth (not smm.toml)
**Why:** The ecosystem has standardised on AGENTS.md. We exploit this instead of fighting it. Ingesting AGENTS.md directly eliminates the translation layer and makes SMM-Sync work with any project that already has AGENTS.md.

### os.rename() for file claiming
**Why:** POSIX-atomic on both macOS and Linux. No external dependency, no daemon required, works in the same filesystem namespace.

### Propose-validate-commit replaces LWW CRDT
**Why:** Silent overwrites destroy trust between agents. Explicit rejection with reasons lets agents surface conflicts to the human. The event log is auditable history.

### events.jsonl is append-only
**Why:** Append-only logs are crash-safe. Any interrupted write produces a malformed last line, not a corrupted file. Replay is deterministic.

### MCP server is the interface boundary
**Why:** MCP is how Claude Code connects to tools. Exposing coordination via MCP means agents can claim files and read context without any special integration — just call the tool.

## Constraints

- Python 3.11+ only
- No network calls except MCP server binding to localhost
- coordinator.py os.rename() logic must not be touched
- state.json must remain human-readable JSON
- Every public function must have a docstring with input/output types
- watcher.py and drift.py are STUBS ONLY

## Danger Zones

- Do not use os.rename() across filesystems — only atomic within the same filesystem
- events.jsonl must never be truncated or rewritten — append only
- The MCP server uses a global _smm_dir — not safe for multi-project serving
- Do not write computed fields into AGENTS.md — it is the source of truth

## Modules

- `cli.py`: Click entry point: init, refresh, status, claim, release, serve
- `config.py`: find_project_root() and get_smm_dir() — project root detection
- `ingester.py`: Parse AGENTS.md into .smm/parsed_context.json
- `state.py`: Propose-validate-commit engine + events.jsonl + state.json
- `coordinator.py`: Tuple Space: os.rename() atomic file claiming in .smm/locks/
- `mcp_server.py`: MCP server with 4 tools: read_context, claim_file, release_file, refresh_context
- `git_utils.py`: Pre-commit hook installation + git diff parsing
- `watcher.py`: STUB: watchdog-based change detection (Month 1)
- `drift.py`: STUB: semantic drift detection (Month 3)

## JIT Context Injection

Before editing any file, call `get_path_context(file_path=<path>)` to surface
constraints and decisions that apply specifically to that part of the codebase.

Examples:
- Editing `src/smm_sync/mcp_server.py` → surfaces MCP security constraints
- Editing `src/smm_sync/coordinator.py` → surfaces atomic locking decisions
- Editing `src/smm_sync/context_graph/client.py` → surfaces graph/embedding rules

The tool returns up to 3 high-confidence results (constraints or score ≥ 0.80).
Zero LLM calls — purely graph similarity search on path-extracted keywords.

## Conventions

- Every public function must have a docstring with Args and Returns
- All .smm/ writes use FileLock when filelock is available
- Comments for adapted code: # adapted from axiom-hub/src/...
- MCP tool functions must have full docstrings (used as tool descriptions)
- Call `get_path_context` before editing any file in the context_graph/ or mcp_server module

## Active Task

**Architecture Revision: AGENTS.md ingestion + event log + MCP server**

Replace smm.toml with AGENTS.md ingestion. Replace LWW state with propose-validate-commit event log. Add MCP server as the interface boundary.

Files in scope:
- src/smm_sync/ingester.py (new)
- src/smm_sync/state.py (rewritten)
- src/smm_sync/mcp_server.py (new)
- src/smm_sync/cli.py (updated)
