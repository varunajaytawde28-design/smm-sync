# smm-sync

> Shared context and coordination for simultaneous AI agents working on the same codebase.

## The Problem

Two Claude Code sessions working on the same codebase simultaneously break each other's work. No existing tool solves this.

## Install

```bash
pip install -e .
```

## Quick Start

```bash
# In your project root:
smm init                    # Creates smm.toml, installs pre-commit hook
# Edit smm.toml with your project details
smm compile                 # Writes CLAUDE.md + .cursorrules + AGENTS.md

# In terminal 1:
smm claim auth.py           # Atomically claim a file
# In terminal 2:
smm claim auth.py           # Fails: already claimed

smm status                  # Shows claimed files and current state
smm release auth.py         # Release when done
```

## How it works

1. You maintain one `smm.toml` with your project's identity, architectural decisions, and active task.
2. `smm compile` renders that into `CLAUDE.md`, `.cursorrules`, and `AGENTS.md` — one file per AI tool.
3. `smm claim <file>` uses `os.rename()` POSIX atomicity to ensure only one agent works on a file at a time.
4. `.smm/state.json` is a CRDT LWW-Register — merge-safe across simultaneous writes.

## Architecture

Three layers, inspired by ant colony stigmergy:

| Layer | File | Purpose |
|-------|------|---------|
| Insight | `AGENTS.md` | Immutable architectural rules. Human-approved only. |
| Pheromone | `.smm/state.json` | Active signals — who owns what, right now. |
| Interaction | `.smm/history/` | Session logs. RAG-accessible. |

## Tests

```bash
pytest tests/
```

## Why not YAML / why not a daemon?

See [docs/WHY.md](docs/WHY.md).
