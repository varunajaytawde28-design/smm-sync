# Why SMM-Sync?

## The Problem

Two Claude Code sessions working on the same codebase simultaneously break each other's work.

This happened to the developer. They had one agent working on the auth module and another working on the API layer. Both had different mental models of the current state. Neither knew what the other was doing. The first agent finished and committed code that directly conflicted with what the second agent had been building for the past hour.

No existing tool solves this:
- `git` tells you what changed *after* the fact
- `CLAUDE.md` is a diary — requires manual input, goes stale immediately
- Linear/Jira are project management tools, not agent coordination protocols

## The Solution

SMM-Sync is a **security camera**, not a diary.

It automatically:
1. Compiles shared context from a single source of truth (`smm.toml`)
2. Broadcasts that context to every agent via `CLAUDE.md`, `.cursorrules`, and `AGENTS.md`
3. Coordinates file ownership using atomic file operations (no server required)
4. Tracks who is working on what via a CRDT-safe shared state

## Why These Design Choices?

### `os.rename()` for file claiming

POSIX atomicity guarantee: on the same filesystem, `os.rename()` is atomic. Either you get the file or you don't. No daemon, no port, no network. Works in CI, works in Docker, works on a laptop.

The alternative — checking if a file exists before writing — has a TOCTOU race. `os.rename()` eliminates the race.

### CRDT LWW-Register for `state.json`

When two agents write state simultaneously, you need a merge strategy. Last-Write-Wins is the simplest CRDT that gives eventual consistency without coordination. For the "who is working on what" use case, the last writer is the correct winner.

### TOML not YAML

`pyproject.toml` has trained every Python developer to think in TOML. No indentation surprises. No `True`/`true` ambiguity. Stdlib support in Python 3.11+ via `tomllib`.

### Three-Layer Architecture

Inspired by ant colony stigmergy:
- **Insight Layer** (AGENTS.md): Immutable architectural rules, human-approved only
- **Pheromone Layer** (state.json): Active signals — who owns what, right now
- **Interaction Layer** (.smm/history/): Session logs, accessible via RAG when needed

Ants don't coordinate by talking to each other. They coordinate by leaving traces in the environment. SMM-Sync is that environment.

## Why Not Axiom Hub?

Axiom Hub required a human to manually write axioms and review conflict reports. It was a diary. It surfaced contradictions after the fact and required human resolution.

SMM-Sync is automatic. `smm compile` runs in a pre-commit hook. Agents don't need to do anything special — they just read the files that already exist.
