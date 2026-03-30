"""Git integration: pre-commit hook installation and git diff parsing."""
from __future__ import annotations

import subprocess
from pathlib import Path

HOOK_SCRIPT = """\
#!/bin/sh
# smm-sync pre-commit hook
# Runs smm check; only recompiles CLAUDE.md/.cursorrules/AGENTS.md if new decisions found.
if command -v smm >/dev/null 2>&1; then
    smm check --quiet 2>/dev/null || true
    if [ -f .smm/.check_dirty ]; then
        smm compile --quiet && git add CLAUDE.md .cursorrules AGENTS.md 2>/dev/null || true
        rm -f .smm/.check_dirty
    fi
fi
"""


def find_git_root(start: Path | None = None) -> Path | None:
    """Walk up from start (or cwd) to find the directory containing .git/.

    Args:
        start: Starting directory. Defaults to cwd.

    Returns:
        Path to directory containing .git/, or None if not in a git repo.
    """
    here = Path(start or Path.cwd()).resolve()
    for candidate in [here, *here.parents]:
        if (candidate / ".git").is_dir():
            return candidate
    return None


def install_pre_commit_hook(project_root: Path) -> bool:
    """Install smm-sync pre-commit hook into .git/hooks/pre-commit.

    Appends to existing hook if one is present. Idempotent.

    Args:
        project_root: Root of the git repository (contains .git/).

    Returns:
        True if installed successfully, False if .git/hooks/ not found.
    """
    git_hooks = project_root / ".git" / "hooks"
    if not git_hooks.exists():
        return False

    hook_path = git_hooks / "pre-commit"

    if hook_path.exists():
        existing = hook_path.read_text(encoding="utf-8")
        if "smm-sync" in existing:
            return True  # Already installed — idempotent
        hook_path.write_text(existing.rstrip() + "\n\n" + HOOK_SCRIPT, encoding="utf-8")
    else:
        hook_path.write_text(HOOK_SCRIPT, encoding="utf-8")

    hook_path.chmod(0o755)
    return True


def get_changed_files(project_root: Path) -> list[str]:
    """Return list of files changed since last commit using git diff.

    Args:
        project_root: Root of the git repository.

    Returns:
        List of relative file paths that have been modified (unstaged).
    """
    try:
        result = subprocess.run(
            ["git", "diff", "--name-only", "HEAD"],
            capture_output=True,
            text=True,
            cwd=project_root,
            timeout=10,
        )
        if result.returncode == 0:
            return [f for f in result.stdout.splitlines() if f]
        return []
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return []


def get_staged_files(project_root: Path) -> list[str]:
    """Return list of files currently staged for the next commit.

    Args:
        project_root: Root of the git repository.

    Returns:
        List of relative file paths that are staged.
    """
    try:
        result = subprocess.run(
            ["git", "diff", "--name-only", "--cached"],
            capture_output=True,
            text=True,
            cwd=project_root,
            timeout=10,
        )
        if result.returncode == 0:
            return [f for f in result.stdout.splitlines() if f]
        return []
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return []


def get_git_remote(project_root: Path, remote: str = "origin") -> "tuple[str, str] | None":
    """Parse GitHub owner and repo name from a git remote URL.

    Handles both HTTPS (https://github.com/owner/repo.git) and SSH
    (git@github.com:owner/repo.git) remote formats.

    Args:
        project_root: Root of the git repository (contains .git/).
        remote: Remote name to inspect. Defaults to 'origin'.

    Returns:
        (owner, repo) tuple, or None if the remote is not found or is not
        a GitHub URL.
    """
    import re

    try:
        result = subprocess.run(
            ["git", "remote", "get-url", remote],
            capture_output=True,
            text=True,
            cwd=project_root,
            timeout=10,
        )
        if result.returncode != 0:
            return None
        url = result.stdout.strip()
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return None

    # HTTPS: https://github.com/owner/repo.git
    m = re.match(r"https://github\.com/([^/]+)/([^/]+?)(?:\.git)?$", url)
    if m:
        return m.group(1), m.group(2)

    # SSH: git@github.com:owner/repo.git
    m = re.match(r"git@github\.com:([^/]+)/([^/]+?)(?:\.git)?$", url)
    if m:
        return m.group(1), m.group(2)

    return None
