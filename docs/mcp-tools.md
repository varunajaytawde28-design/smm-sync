# MCP Tools Reference

Tools exposed by the Axiom Hub MCP server (`smm serve`). All tools except `get_project_context` require a session to be initialized first. Back to [README](../README.md).

**v1 scope:** The core decision loop — `get_project_context`, `add_decision`, `complete_session`, and `resolve_contradiction` — is fully tested and production-validated. The remaining tools support advanced workflows (natural language search, constraint enforcement, multi-agent file coordination) that are functional but not yet validated end-to-end. They ship as-is for early adopters to explore; deeper validation is planned for v1.1.

---

## `get_project_context`

**Must be called first in every session.** Returns active decisions, unresolved contradictions, constraints, and a session token. Creates the session lock file that unblocks the PreToolUse hook.

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `project` | `str` | `"smm-sync"` | Project name |
| `session_id` | `str` | `""` | Optional session identifier (used for kill check) |

**Returns:** Structured string containing mandatory protocol, all decisions with rationales, unresolved contradictions with resolve commands, recently resolved contradictions (30 days), AGENTS.md content, and a session token UUID.

**Flow:**
1. Creates session lock file at `/tmp/smm-session-<hash>.lock`
2. Generates a session token (UUID)
3. Runs architectural violation scan (grep for superseded patterns)
4. Loads decisions from `decisions.jsonl` (falls back to Kuzu graph)
5. Loads unresolved contradictions from `contradictions.jsonl`
6. Builds structured response with mandatory protocol

---

## `add_decision`

Record an architectural, technical, product, or constraint decision. Always writes to `decisions.jsonl` (critical path). Graph sync is optional and non-blocking on failure.

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `title` | `str` | required | Short decision title |
| `content` | `str` | `""` | Full description (alias: `description`) |
| `rationale` | `str` | `""` | Why this decision was made |
| `made_by` | `str` | `""` | Who made it (defaults to `"agent"`) |
| `project` | `str` | `"smm-sync"` | Project name |
| `constraints` | `list[str]` | `[]` | Known constraints imposed |
| `alternatives` | `list[str]` | `[]` | Alternatives considered |
| `decision_type` | `str` | `"technical"` | `architectural` / `technical` / `product` / `constraint` (alias: `type`) |
| `confidence` | `float\|None` | `None` | Confidence score 0.0–1.0 |
| `session_token` | `str` | `""` | Session token from `get_project_context` |

**Returns:** `{"success": bool, "decision_id": str}`

---

## `complete_session`

Close the current work session and summarize decisions captured. Must be called after implementation work is done, before committing. Fires a background `smm check --quiet` so the next session picks up new contradictions.

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `session_token` | `str` | required | UUID returned by `get_project_context` |

**Returns:** `"Session {token} captured {N} decisions across {M} files. Review these modified files for missed decisions: [list]"`

**Flow:**
1. Counts decisions in `decisions.jsonl` matching the session token
2. Collects modified files via `git diff`, `git diff --cached`, and `git ls-files --others`
3. Logs session close to compliance lineage
4. Spawns background `smm check --quiet`

---

## `resolve_contradiction`

Resolve a detected contradiction. **Requires explicit developer confirmation** — the agent must ask the developer which option to keep and have them type YES to confirm. Never auto-resolves based on PRD context alone.

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `id` | `str` | required | Contradiction UUID |
| `winner` | `str` | required | `"a"` (keep decision A), `"b"` (keep decision B), or `"dismiss"` |
| `note` | `str` | `""` | Developer comment |
| `confirmation` | `str` | required | Must be exactly `"YES"` |

**Returns:** Confirmation string with resolution outcome and count of remaining unresolved contradictions.

**Flow:**
1. Validates `confirmation == "YES"` (raises ValueError otherwise)
2. Finds target in `contradictions.jsonl` by UUID
3. Sets status to `resolved` or `dismissed`, records winner/loser
4. Rewrites `contradictions.jsonl` atomically (write to `.tmp`, then rename)
5. Writes SHA-256 hash-chained audit event to `compliance_lineage.jsonl`

---

## `check_contradictions`

Run `smm check` inline to sync new decisions and detect contradictions.

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `project` | `str` | `"smm-sync"` | Project name |

**Returns:** `{"synced": int, "contradictions_found": int, "unresolved": list[dict], "dirty": bool, "message": str, "display": str}`

---

## `query_decisions` *(advanced — not yet validated end-to-end)*

Search team decisions and architectural knowledge. Includes "Deja Vu" detection — warns if a query resembles previously-rejected alternatives (zero LLM calls, graph similarity only).

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `query` | `str` | required | Natural language question (e.g. "why did we choose Kuzu?") |
| `project` | `str` | `"smm-sync"` | Project name |
| `limit` | `int` | `5` | Max results |
| `session_id` | `str` | `""` | Session identifier (used for kill check) |

**Returns:** Formatted string of relevant decisions with rationale and time-saved footer.

---

## `check_constraints` *(advanced — not yet validated end-to-end)*

Check if a proposed action violates any known project constraints.

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `proposed_action` | `str` | required | What you are about to do (e.g. "replace Kuzu with FalkorDB") |
| `project` | `str` | `"smm-sync"` | Project name |
| `session_id` | `str` | `""` | Session identifier |

**Returns:** `{"conflicts": list[str], "warnings": list[str], "clear": bool}`

---

## `add_constraint` *(advanced — not yet validated end-to-end)*

Register a non-negotiable project constraint. Constraints are different from decisions — they are rules that must never be violated. Stored as decisions with title prefixed by `[CONSTRAINT]`.

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `constraint` | `str` | required | Constraint rule in one clear sentence |
| `scope_keywords` | `list[str]` | required | Keywords that trigger this constraint to surface |
| `rationale` | `str` | required | Why this constraint exists |
| `project` | `str` | `"smm-sync"` | Project name |

**Returns:** `{"success": bool, "constraint_id": str}`

---

## `get_decision_timeline` *(advanced — not yet validated end-to-end)*

Chronological history of decisions related to a topic, showing how team thinking evolved including superseded decisions.

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `topic` | `str` | required | Natural language topic (e.g. "state management", "database choice") |
| `project` | `str` | `"smm-sync"` | Project name |

**Returns:** Timeline string with ACTIVE/SUPERSEDED markers, timestamps, content, and superseded notes.

---

## `get_compliance_lineage`

Audit trail for EU AI Act compliance and SOC 2 AI governance audits. Answers: "What did the AI know when it made this decision?"

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `session_id` | `str\|None` | `None` | Filter — shows all context injected into that session |
| `decision_title` | `str\|None` | `None` | Filter — shows all times this decision was surfaced |

**Returns:** Formatted audit trail string (capped at 50 displayed entries). Shows all entries if neither filter is provided.

---

## `get_path_context` *(advanced — not yet validated end-to-end)*

Just-in-time context for the file being edited. Returns constraints and high-confidence decisions relevant to that specific file path. Zero LLM calls — purely graph similarity search on path-extracted keywords. Returns up to 3 results (constraints or score >= 0.80).

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `file_path` | `str` | required | Relative or absolute path to the file being edited |
| `project` | `str` | `"smm-sync"` | Project name |

**Returns:** Formatted string of relevant rules, or "No specific rules found".

---

## `get_board_items`

Read the kanban-style decision board from `.smm/board.json`.

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `status` | `str` | `""` | Filter: `"backlog"`, `"in_progress"`, `"done"`, or empty for all |

**Returns:** Formatted markdown list of board items with status icons.

---

## `update_board_item`

Create or update an item on the decision board. Status flow: `backlog` -> `in_progress` -> `done`.

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `title` | `str` | `""` | Short title for the board item |
| `status` | `str` | `"backlog"` | One of `backlog`, `in_progress`, `done` |
| `description` | `str` | `""` | Optional longer description |
| `item_id` | `str` | `""` | If provided, update existing item; if empty, create new |

**Returns:** `{"success": bool, "id": str, "action": "created"|"updated"}`

---

## `read_context`

Return current `AGENTS.md` content plus active coordination state (claimed files, active sessions). Agents call this at session start to get full project context.

No parameters.

**Returns:** Formatted string combining AGENTS.md content with live state.

---

## `claim_file` *(advanced — not yet validated end-to-end)*

Atomically claim a file for exclusive editing. Uses propose-validate-commit.

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `filepath` | `str` | required | Relative path of file to claim |
| `session_id` | `str` | required | Unique identifier for this agent session |
| `task` | `str` | `""` | Optional description of what this agent is doing |

**Returns:** `{"success": bool, "conflict": str}` (conflict only present on failure with current owner info)

---

## `release_file` *(advanced — not yet validated end-to-end)*

Release a claimed file after completing edits.

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `filepath` | `str` | required | Relative path of file to release |
| `session_id` | `str` | required | Identifier of the session that claimed the file |

**Returns:** `{"success": bool, "reason": str}`

---

## `refresh_context`

Check if AGENTS.md changed; re-parse if so. Agents call this after a git commit lands to pick up context updates.

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `session_id` | `str` | required | Identifier of the calling session |

**Returns:** `{"changed": bool, "context": str}` or `{"changed": bool, "reason": str}`

---

## Configuration

See [CLI Reference — Configuration](cli.md#configuration) for environment variables and `.smm/` directory layout.
