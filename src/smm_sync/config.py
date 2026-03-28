"""Load and validate smm.toml using tomllib (Python 3.11+ stdlib).

Context graph configuration:
    GRAPH_DIR: Path to Kuzu graph database (.smm/graph/ relative to project root).
    GRAPH_PROJECT_DEFAULT: Default project name for graph partitioning.
    ANTHROPIC_API_KEY: Anthropic API key from environment (required for seeding).
"""
from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

# Context graph configuration — resolved at import time
GRAPH_PROJECT_DEFAULT: str = "smm-sync"
ANTHROPIC_API_KEY: str = os.environ.get("ANTHROPIC_API_KEY", "")

# Dashboard configuration
DEFAULT_DASHBOARD_PORT: int = 7842
DASHBOARD_PORT: int = int(os.environ.get("SMM_DASHBOARD_PORT", DEFAULT_DASHBOARD_PORT))


def get_graph_dir(start: Path | None = None) -> Path:
    """Return .smm/graph/ directory path for the current project.

    Args:
        start: Starting directory. Defaults to cwd.

    Returns:
        Path to .smm/graph/ directory (may not exist yet).
    """
    return get_smm_dir(start) / "graph"


def find_project_root(start: Path | None = None) -> Path:
    """Walk up from start (or cwd) looking for .smm/ or .git/.

    Returns the directory containing the marker. Falls back to cwd.

    Args:
        start: Starting directory. Defaults to cwd.

    Returns:
        Path to project root directory.
    """
    here = Path(start or Path.cwd()).resolve()
    for candidate in [here, *here.parents]:
        if (candidate / ".smm").is_dir():
            return candidate
        if (candidate / ".git").is_dir():
            return candidate
    return here


def get_smm_dir(start: Path | None = None) -> Path:
    """Return .smm/ directory path for the current project.

    Args:
        start: Starting directory. Defaults to cwd.

    Returns:
        Path to .smm/ directory (may not exist yet).
    """
    return find_project_root(start) / ".smm"


@dataclass
class ArchDecision:
    """A single architectural decision with what was decided and why."""
    what: str
    why: str


@dataclass
class Module:
    """A module entry in the module map."""
    name: str
    description: str
    owner: str = ""


@dataclass
class SmmConfig:
    """Validated configuration loaded from smm.toml."""
    # [project]
    name: str
    purpose: str
    stack: list[str] = field(default_factory=list)

    # [active_task]
    title: str = ""
    description: str = ""
    files_in_scope: list[str] = field(default_factory=list)

    # [arch_decisions]
    decisions: list[ArchDecision] = field(default_factory=list)

    # [constraints]
    known: list[str] = field(default_factory=list)

    # [danger_zones]
    zones: list[str] = field(default_factory=list)

    # [modules]
    modules: list[Module] = field(default_factory=list)

    # [conventions]
    conventions: list[str] = field(default_factory=list)


def load_config(path: Path) -> SmmConfig:
    """Load and validate smm.toml from the given path.

    Args:
        path: Path to smm.toml file.

    Returns:
        Validated SmmConfig dataclass instance.

    Raises:
        FileNotFoundError: If smm.toml does not exist at path.
        ValueError: If required fields are missing or invalid.
    """
    if not path.exists():
        raise FileNotFoundError(f"smm.toml not found at {path}")

    with open(path, "rb") as f:
        raw = tomllib.load(f)

    project = raw.get("project", {})
    if not project.get("name"):
        raise ValueError("smm.toml: [project].name is required")
    if not project.get("purpose"):
        raise ValueError("smm.toml: [project].purpose is required")

    decisions = []
    for d in raw.get("arch_decisions", {}).get("decisions", []):
        decisions.append(ArchDecision(what=d["what"], why=d["why"]))

    modules = []
    for m in raw.get("modules", {}).get("items", []):
        modules.append(Module(
            name=m["name"],
            description=m.get("description", ""),
            owner=m.get("owner", ""),
        ))

    active = raw.get("active_task", {})
    constraints = raw.get("constraints", {})
    danger = raw.get("danger_zones", {})
    conventions = raw.get("conventions", {})

    return SmmConfig(
        name=project["name"],
        purpose=project["purpose"],
        stack=project.get("stack", []),
        title=active.get("title", ""),
        description=active.get("description", ""),
        files_in_scope=active.get("files_in_scope", []),
        decisions=decisions,
        known=constraints.get("known", []),
        zones=danger.get("zones", []),
        modules=modules,
        conventions=conventions.get("items", []),
    )
