"""SMM Lore-Hook: hook script templates and installation utilities.

Installs two git hooks + optional Claude Code/Cursor agent hooks that
automatically capture architectural decisions at commit time.

Flow:
  git commit
    → prepare-commit-msg hook → pre-commit-capture.sh
      → classifies diff via claude -p (Haiku)
      → if decision: contradictions check, user dialog, trailer injection
      → graph ingestion in background via smm add-decision

Claude Code PreToolUse hook fires BEFORE git commit runs — same script,
but writes trailers to .git/SMM_TRAILERS for prepare-commit-msg to inject.
"""
from __future__ import annotations

import json
import os
import stat
import subprocess
from pathlib import Path

# ---------------------------------------------------------------------------
# Shell script: pre-commit-capture.sh
# ---------------------------------------------------------------------------
# Design notes:
#   - Uses temp files for all inter-process data (safe quoting, no injection)
#   - Python3 for JSON parsing (always available in smm-sync virtualenv)
#   - claude -p with --model haiku (fast, cheap, <2s for "no decision" path)
#   - Never exits non-zero — commits always proceed
#   - prepare-commit-msg mode ($1 = msg file): injects trailers directly
#   - Standalone mode (Claude Code PreToolUse): writes .git/SMM_TRAILERS
# ---------------------------------------------------------------------------

CAPTURE_SCRIPT = r"""#!/usr/bin/env bash
# ─── SMM Lore-Hook: pre-commit-capture.sh ────────────────────────────────
# Classifies git diffs for architectural decisions, injects Git trailers,
# and ingests into the Axiom knowledge graph.
#
# Invocation modes:
#   1. Git prepare-commit-msg hook: pre-commit-capture.sh <msg-file>
#   2. Claude Code PreToolUse hook: pre-commit-capture.sh (no args)
#
# Never exits non-zero. Skips silently if:
#   - No staged changes
#   - ANTHROPIC_API_KEY not set
#   - claude CLI not found
#   - smm not found
# ────────────────────────────────────────────────────────────────────────────

SMM_HOOK_DIR="$HOME/.smm"
LOG_FILE="$SMM_HOOK_DIR/lore-hook.log"
MAX_DIFF_CHARS=3500

# Create log dir
mkdir -p "$SMM_HOOK_DIR"

log() { echo "[$(date -u '+%H:%M:%S')] $*" >> "$LOG_FILE" 2>/dev/null || true; }

# ── Mode detection ────────────────────────────────────────────────────────
# prepare-commit-msg mode: $1 is the commit message file
COMMIT_MSG_FILE=""
if [ -n "${1:-}" ] && [ -f "${1:-}" ]; then
    COMMIT_MSG_FILE="$1"
fi

log "lore-hook triggered (mode=${COMMIT_MSG_FILE:+prepare-commit-msg}${COMMIT_MSG_FILE:-standalone})"

# ── Guard: ANTHROPIC_API_KEY ──────────────────────────────────────────────
if [ -z "${ANTHROPIC_API_KEY:-}" ]; then
    [ -f "$SMM_HOOK_DIR/config" ] && . "$SMM_HOOK_DIR/config" 2>/dev/null || true
fi
if [ -z "${ANTHROPIC_API_KEY:-}" ]; then
    log "ANTHROPIC_API_KEY not set — skipping"
    exit 0
fi

# ── Guard: claude CLI ─────────────────────────────────────────────────────
if ! command -v claude >/dev/null 2>&1; then
    log "claude CLI not found — skipping"
    exit 0
fi

# ── Guard: staged changes ─────────────────────────────────────────────────
if git diff --staged --quiet 2>/dev/null; then
    log "no staged changes — skipping"
    exit 0
fi

# ── Temp files (cleaned up on exit) ──────────────────────────────────────
DIFF_FILE=$(mktemp smm_diff_XXXXXX.txt)
PROMPT_FILE=$(mktemp smm_prompt_XXXXXX.txt)
RESPONSE_FILE=$(mktemp smm_response_XXXXXX.json)
PAYLOAD_FILE=$(mktemp smm_payload_XXXXXX.json)
trap "rm -f $DIFF_FILE $PROMPT_FILE $RESPONSE_FILE $PAYLOAD_FILE" EXIT

# ── Get staged diff (truncated) ───────────────────────────────────────────
git diff --staged --unified=3 2>/dev/null | head -c "$MAX_DIFF_CHARS" > "$DIFF_FILE"
if [ ! -s "$DIFF_FILE" ]; then
    log "empty diff — skipping"
    exit 0
fi

# ── Build classification prompt ───────────────────────────────────────────
python3 - "$DIFF_FILE" > "$PROMPT_FILE" << 'PYEOF'
import sys
diff = open(sys.argv[1]).read()

print('''You are an architectural decision classifier. Analyze this git diff.

An architectural decision is a significant choice about: system design, \
tech stack, libraries, protocols, data models, security, deployment, or \
product direction. NOT: bug fixes, style changes, dependency bumps, or test updates.

Respond ONLY with valid JSON (no markdown, no explanation):
- If NO decision: {"is_decision":false}
- If YES decision: {"is_decision":true,"title":"<80 chars>","rationale":"<why this approach>","alternatives":["<alt1>"],"constraints":["<constraint1>"],"type":"architectural|technical|product","confidence":0.85}

Git diff:
---
''' + diff + "\n---")
PYEOF

# ── Call Claude (Haiku — fast, cheap) ─────────────────────────────────────
log "calling claude haiku for classification..."
claude --print --model "claude-haiku-4-5-20251001" < "$PROMPT_FILE" > "$RESPONSE_FILE" 2>/dev/null || {
    log "claude call failed — skipping"
    exit 0
}

# ── Parse response ─────────────────────────────────────────────────────────
python3 - "$RESPONSE_FILE" > "$PAYLOAD_FILE" << 'PYEOF'
import sys, json, re

raw = open(sys.argv[1]).read().strip()

# Strip markdown code fences if present
raw = re.sub(r'^```(?:json)?\s*', '', raw, flags=re.MULTILINE)
raw = re.sub(r'```\s*$', '', raw, flags=re.MULTILINE)

# Find JSON object
m = re.search(r'\{.*\}', raw, re.DOTALL)
if not m:
    print(json.dumps({"is_decision": False}))
    sys.exit(0)

try:
    d = json.loads(m.group(0))
    print(json.dumps(d))
except Exception:
    print(json.dumps({"is_decision": False}))
PYEOF

IS_DECISION=$(python3 -c "import json,sys; d=json.load(open(sys.argv[1])); print(d.get('is_decision',False))" "$PAYLOAD_FILE" 2>/dev/null) || IS_DECISION="False"

if [ "$IS_DECISION" != "True" ]; then
    log "no decision detected — commit proceeds normally"
    exit 0
fi

log "decision detected — checking contradictions"

# ── Extract fields ─────────────────────────────────────────────────────────
TITLE=$(python3 -c "import json,sys; d=json.load(open(sys.argv[1])); print(d.get('title','Unnamed decision')[:80])" "$PAYLOAD_FILE" 2>/dev/null)
RATIONALE=$(python3 -c "import json,sys; d=json.load(open(sys.argv[1])); print(d.get('rationale',''))" "$PAYLOAD_FILE" 2>/dev/null)
DTYPE=$(python3 -c "import json,sys; d=json.load(open(sys.argv[1])); print(d.get('type','technical'))" "$PAYLOAD_FILE" 2>/dev/null)
CONFIDENCE=$(python3 -c "import json,sys; d=json.load(open(sys.argv[1])); print(d.get('confidence',0.80))" "$PAYLOAD_FILE" 2>/dev/null)

# ── Contradiction check ───────────────────────────────────────────────────
# Only NEW pairs are shown (already-resolved/deferred/ignored pairs are
# filtered by smm check-contradictions against .smm/contradiction_index.json).
# smm handle-contradictions shows an [R]esolve/[D]efer/[I]gnore menu for
# each new pair, records every action in the index, and outputs the overall
# status (approved/deferred) to stdout.
STATUS="approved"
if command -v smm >/dev/null 2>&1; then
    CONTRA_FILE=$(mktemp smm_contra_XXXXXX.json)
    trap "rm -f $DIFF_FILE $PROMPT_FILE $RESPONSE_FILE $PAYLOAD_FILE $CONTRA_FILE" EXIT

    smm check-contradictions --title "$TITLE" --content "$RATIONALE" --json-output \
        > "$CONTRA_FILE" 2>/dev/null || echo '{"contradictions":[]}' > "$CONTRA_FILE"

    CONTRA_COUNT=$(python3 -c "
import json,sys
d=json.load(open(sys.argv[1]))
print(len(d.get('contradictions',[])))
" "$CONTRA_FILE" 2>/dev/null) || CONTRA_COUNT="0"

    if [ "${CONTRA_COUNT:-0}" -gt 0 ] 2>/dev/null; then
        # Detect CI / non-interactive environment
        NON_INTERACTIVE_FLAG=""
        if [ "${CI:-}" = "true" ] || [ "${CI:-}" = "1" ] \
           || [ "${AXIOM_NON_INTERACTIVE:-}" = "1" ] \
           || ! [ -c /dev/tty ] 2>/dev/null; then
            NON_INTERACTIVE_FLAG="--non-interactive"
            log "non-interactive mode — all contradictions deferred"
        fi

        # handle-contradictions prints interactive prompts to stderr
        # (redirected to /dev/tty by 2>/dev/tty) and prints the overall
        # status to stdout for capture in STATUS.
        STATUS=$(smm handle-contradictions \
            --title "$TITLE" \
            --contra-file "$CONTRA_FILE" \
            $NON_INTERACTIVE_FLAG \
            2>/dev/tty) || STATUS="deferred"

        log "contradictions handled — overall status: $STATUS"
    fi
fi

# ── Confidence label ───────────────────────────────────────────────────────
CONF_LABEL=$(python3 -c "
c=float('$CONFIDENCE')
print('high' if c>=0.85 else ('medium' if c>=0.70 else 'low'))
" 2>/dev/null) || CONF_LABEL="medium"

ALTERNATIVES=$(python3 -c "
import json,sys
d=json.load(open(sys.argv[1]))
print('; '.join(d.get('alternatives',[])))
" "$PAYLOAD_FILE" 2>/dev/null)

# ── Inject Git trailers ───────────────────────────────────────────────────
write_trailers() {
    local FILE="$1"
    printf '\n' >> "$FILE"
    printf 'Axiom-Decision: %s\n' "$TITLE" >> "$FILE"
    printf 'Axiom-Rationale: %s\n' "$RATIONALE" >> "$FILE"
    [ -n "$ALTERNATIVES" ] && printf 'Axiom-Alternative: %s\n' "$ALTERNATIVES" >> "$FILE"
    printf 'Axiom-Confidence: %s\n' "$CONF_LABEL" >> "$FILE"
    printf 'Axiom-Type: %s\n' "$DTYPE" >> "$FILE"
    printf 'Axiom-Status: %s\n' "$STATUS" >> "$FILE"
}

if [ -n "$COMMIT_MSG_FILE" ]; then
    # prepare-commit-msg mode: inject directly
    write_trailers "$COMMIT_MSG_FILE"
    log "trailers injected into commit message"
else
    # Standalone/PreToolUse mode: write for prepare-commit-msg to pick up
    GIT_DIR=$(git rev-parse --git-dir 2>/dev/null) || GIT_DIR=".git"
    TRAILERS_PENDING="$GIT_DIR/SMM_TRAILERS"
    write_trailers "$TRAILERS_PENDING"
    log "trailers staged at $TRAILERS_PENDING"
fi

# ── Write decision to JSONL (fast path — Rust binary or Python fallback) ───
# smm add-decision writes to .smm/decisions.jsonl only (~10ms Rust / <500ms Python).
# smm check runs in the background afterwards; the commit never blocks on it.
if command -v smm >/dev/null 2>&1; then
    # Capture git context for EU AI Act Art 12 reference source traceability
    GIT_HASH=$(git rev-parse --short HEAD 2>/dev/null) || GIT_HASH=""
    GIT_BRANCH=$(git rev-parse --abbrev-ref HEAD 2>/dev/null) || GIT_BRANCH=""
    if [ -n "$COMMIT_MSG_FILE" ]; then
        COMMIT_TRIGGER=$(head -1 "$COMMIT_MSG_FILE" 2>/dev/null) || COMMIT_TRIGGER="$TITLE"
    else
        COMMIT_TRIGGER="$TITLE"
    fi
    GIT_HASH="$GIT_HASH" GIT_BRANCH="$GIT_BRANCH" COMMIT_TRIGGER="$COMMIT_TRIGGER" \
    python3 - "$PAYLOAD_FILE" "$STATUS" << 'PYEOF' | smm add-decision - >> "$LOG_FILE" 2>&1
import json, sys, os
d = json.load(open(sys.argv[1]))
payload = {
    "title": d.get("title", ""),
    "rationale": d.get("rationale", ""),
    "type": d.get("type", "technical"),
    "alternatives": d.get("alternatives", []),
    "constraints": d.get("constraints", []),
    "confidence": d.get("confidence", 0.80),
    "made_by": "git-commit (lore-hook)",
    "source": "manual",
    "context": {
        "source": "git-commit",
        "trigger": os.environ.get("COMMIT_TRIGGER", "")[:200],
        "git_ref": os.environ.get("GIT_HASH", ""),
        "branch": os.environ.get("GIT_BRANCH", ""),
    },
}
print(json.dumps(payload))
PYEOF
    log "decision written to JSONL (status=$STATUS)"

    # ── Run contradiction check in background — commit proceeds instantly ────
    # smm check takes ~15s (LLM call). Running it detached means the developer
    # is never blocked. Results land in .smm/contradictions.jsonl and are
    # surfaced on the next `smm get-context` call or dashboard refresh.
    smm check >> "$LOG_FILE" 2>&1 &
    SMM_CHECK_PID=$!
    disown $SMM_CHECK_PID 2>/dev/null || true
    echo "🔍 Contradiction check running in background..." > /dev/tty
    echo "   Results will appear on next session start or in the dashboard." > /dev/tty
    log "background smm check launched (pid=$SMM_CHECK_PID)"
fi

log "lore-hook complete — commit proceeds instantly"
exit 0
"""


# ---------------------------------------------------------------------------
# prepare-commit-msg hook: picks up .git/SMM_TRAILERS from PreToolUse run
# ---------------------------------------------------------------------------
PREPARE_COMMIT_MSG_HOOK = """\
#!/bin/sh
# SMM Lore-Hook: inject pre-staged trailers into commit message.
# Trailers are written by pre-commit-capture.sh when run in standalone mode
# (e.g., Claude Code PreToolUse hook fires before git commit).
GIT_DIR=$(git rev-parse --git-dir 2>/dev/null) || GIT_DIR=".git"
TRAILERS_FILE="$GIT_DIR/SMM_TRAILERS"
if [ -f "$TRAILERS_FILE" ]; then
    cat "$TRAILERS_FILE" >> "$1"
    rm -f "$TRAILERS_FILE"
fi
"""


def install_capture_script(smm_hooks_dir: Path | None = None) -> Path:
    """Write pre-commit-capture.sh to ~/.smm/hooks/ and chmod +x.

    Args:
        smm_hooks_dir: Override default ~/.smm/hooks/. Used for testing.

    Returns:
        Path to the installed script.
    """
    hooks_dir = smm_hooks_dir or (Path.home() / ".smm" / "hooks")
    hooks_dir.mkdir(parents=True, exist_ok=True)

    script_path = hooks_dir / "pre-commit-capture.sh"
    script_path.write_text(CAPTURE_SCRIPT, encoding="utf-8")
    script_path.chmod(script_path.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    return script_path


def install_git_hooks(git_root: Path) -> bool:
    """Install prepare-commit-msg git hook (for trailer injection).

    Installs a hook that:
    1. Runs pre-commit-capture.sh for classification + ingestion
    2. Picks up any trailers written by a prior PreToolUse run

    Idempotent — safe to call multiple times.

    Args:
        git_root: Root of the git repository (contains .git/).

    Returns:
        True on success, False if .git/hooks/ not found.
    """
    git_hooks = git_root / ".git" / "hooks"
    if not git_hooks.exists():
        return False

    capture_script = Path.home() / ".smm" / "hooks" / "pre-commit-capture.sh"

    # prepare-commit-msg: runs capture script + picks up pre-staged trailers
    pcm_hook = git_hooks / "prepare-commit-msg"
    pcm_content = f"""\
#!/bin/sh
# smm-sync SMM Lore-Hook
# Classifies diff for architectural decisions, injects trailers, ingests to graph.
if [ -x "{capture_script}" ]; then
    "{capture_script}" "$1" "$2" "$3"
fi
{PREPARE_COMMIT_MSG_HOOK.strip()}
"""
    if pcm_hook.exists():
        existing = pcm_hook.read_text(encoding="utf-8")
        if "SMM Lore-Hook" not in existing:
            pcm_hook.write_text(existing.rstrip() + "\n\n" + pcm_content, encoding="utf-8")
    else:
        pcm_hook.write_text(pcm_content, encoding="utf-8")
    pcm_hook.chmod(pcm_hook.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)

    return True


def configure_git_trailers(project_root: Path) -> None:
    """Configure git trailer keys for Axiom trailers in this repo.

    Sets trailer.*.key git config so 'git interpret-trailers' and
    'git log --trailer' parse Axiom-* trailers correctly.

    Args:
        project_root: Root of the git repository.
    """
    trailers = {
        "decision": "Axiom-Decision",
        "rationale": "Axiom-Rationale",
        "alternative": "Axiom-Alternative",
        "confidence": "Axiom-Confidence",
        "axiomtype": "Axiom-Type",
        "axiomstatus": "Axiom-Status",
    }
    for key, value in trailers.items():
        try:
            subprocess.run(
                ["git", "config", f"trailer.{key}.key", value],
                cwd=project_root,
                capture_output=True,
                timeout=10,
            )
        except (subprocess.TimeoutExpired, FileNotFoundError):
            pass


def configure_claude_code_hook() -> bool:
    """Add Axiom Lore-Hook PreToolUse entry to ~/.claude/settings.json.

    Adds a PreToolUse hook that fires when Claude Code is about to run
    any 'git commit' bash command. Idempotent — safe to call multiple times.

    Returns:
        True if settings were updated or already correct, False on error.
    """
    settings_path = Path.home() / ".claude" / "settings.json"
    capture_script = str(Path.home() / ".smm" / "hooks" / "pre-commit-capture.sh")

    # Read existing settings
    if settings_path.exists():
        try:
            settings = json.loads(settings_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            settings = {}
    else:
        settings_path.parent.mkdir(parents=True, exist_ok=True)
        settings = {}

    # Check if already configured
    hooks = settings.setdefault("hooks", {})
    pre_tool_use = hooks.setdefault("PreToolUse", [])
    for entry in pre_tool_use:
        if entry.get("matcher") == "Bash(git commit*)":
            return True  # Already installed

    pre_tool_use.append({
        "matcher": "Bash(git commit*)",
        "hooks": [
            {
                "type": "command",
                "command": f"bash {capture_script}",
            }
        ],
    })

    try:
        settings_path.write_text(
            json.dumps(settings, indent=2) + "\n",
            encoding="utf-8",
        )
        return True
    except OSError:
        return False


def configure_cursor_hook(project_root: Path) -> bool:
    """Write .cursor/hooks.json for Cursor IDE agent hook integration.

    Creates .cursor/hooks.json in the project root. This file is read
    by Cursor's beforeShellExecution hook to intercept git commit commands.

    Args:
        project_root: Root of the project (where .cursor/ will be created).

    Returns:
        True on success, False on error.
    """
    cursor_dir = project_root / ".cursor"
    capture_script = str(Path.home() / ".smm" / "hooks" / "pre-commit-capture.sh")

    hooks_config = {
        "version": 1,
        "hooks": {
            "beforeShellExecution": [
                {
                    "matcher": "git commit",
                    "command": f"bash {capture_script}",
                }
            ]
        },
    }

    try:
        cursor_dir.mkdir(exist_ok=True)
        hooks_path = cursor_dir / "hooks.json"
        if not hooks_path.exists():
            hooks_path.write_text(
                json.dumps(hooks_config, indent=2) + "\n",
                encoding="utf-8",
            )
        return True
    except OSError:
        return False
