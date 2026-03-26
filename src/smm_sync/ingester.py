"""Parse AGENTS.md into structured internal state.

AGENTS.md is the source of truth. This module reads it and produces
a structured dict stored at .smm/parsed_context.json.
"""
from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any


_KNOWN_SECTIONS = {
    "project", "architecture", "active task", "modules",
    "constraints", "danger zones", "conventions",
}

_PARSED_CONTEXT_FILE = "parsed_context.json"


def _md5(text: str) -> str:
    """Return MD5 hex digest of text.

    Args:
        text: Input string.

    Returns:
        32-character hex string.
    """
    return hashlib.md5(text.encode("utf-8")).hexdigest()


def parse_agents_md(content: str) -> dict[str, Any]:
    """Parse AGENTS.md content into a structured dict.

    Sections are identified by ## headers. All sections are optional.
    Text between headers is captured as raw markdown strings.

    Args:
        content: Full text content of AGENTS.md.

    Returns:
        Dict with keys for each recognised section (lowercased, spaces→underscores):
            project (str), architecture (str), active_task (str),
            modules (str), constraints (str), danger_zones (str),
            conventions (str), content_hash (str).
    """
    sections: dict[str, str] = {}
    current_section: str | None = None
    current_lines: list[str] = []

    for line in content.splitlines():
        header_match = re.match(r"^##\s+(.+)$", line)
        if header_match:
            if current_section is not None:
                sections[current_section] = "\n".join(current_lines).strip()
            current_section = header_match.group(1).strip().lower()
            current_lines = []
        else:
            if current_section is not None:
                current_lines.append(line)

    if current_section is not None:
        sections[current_section] = "\n".join(current_lines).strip()

    return {
        "project": sections.get("project", ""),
        "architecture": sections.get("architecture", ""),
        "active_task": sections.get("active task", ""),
        "modules": sections.get("modules", ""),
        "constraints": sections.get("constraints", ""),
        "danger_zones": sections.get("danger zones", ""),
        "conventions": sections.get("conventions", ""),
        "content_hash": _md5(content),
    }


def ingest(smm_dir: Path, agents_md_path: Path) -> dict[str, Any]:
    """Parse AGENTS.md and store result in .smm/parsed_context.json.

    Args:
        smm_dir: Path to .smm directory.
        agents_md_path: Path to the AGENTS.md file to parse.

    Returns:
        Parsed context dict (same as parse_agents_md output).

    Raises:
        FileNotFoundError: If agents_md_path does not exist.
    """
    if not agents_md_path.exists():
        raise FileNotFoundError(f"AGENTS.md not found at {agents_md_path}")

    content = agents_md_path.read_text(encoding="utf-8")
    parsed = parse_agents_md(content)

    out_path = smm_dir / _PARSED_CONTEXT_FILE
    out_path.write_text(json.dumps(parsed, indent=2), encoding="utf-8")

    return parsed


def load_parsed_context(smm_dir: Path) -> dict[str, Any]:
    """Load the cached parsed context from .smm/parsed_context.json.

    Args:
        smm_dir: Path to .smm directory.

    Returns:
        Parsed context dict, or empty dict if file does not exist.
    """
    path = smm_dir / _PARSED_CONTEXT_FILE
    if not path.exists():
        return {}
    text = path.read_text(encoding="utf-8").strip()
    if not text:
        return {}
    return json.loads(text)


def migrate_smm_toml(smm_toml_path: Path, output_path: Path) -> bool:
    """Migrate smm.toml content into AGENTS.md format.

    Reads smm.toml, converts to AGENTS.md sections, writes output.
    Prints a one-time migration message. Idempotent: skips if
    output_path already exists.

    Args:
        smm_toml_path: Path to existing smm.toml.
        output_path: Path to write the migrated AGENTS.md.

    Returns:
        True if migration was performed, False if skipped.
    """
    if not smm_toml_path.exists():
        return False
    if output_path.exists():
        return False

    import tomllib

    with open(smm_toml_path, "rb") as f:
        raw = tomllib.load(f)

    project = raw.get("project", {})
    name = project.get("name", smm_toml_path.parent.name)
    purpose = project.get("purpose", "")
    stack = project.get("stack", [])

    decisions = raw.get("arch_decisions", {}).get("decisions", [])
    constraints = raw.get("constraints", {}).get("known", [])
    zones = raw.get("danger_zones", {}).get("zones", [])
    modules = raw.get("modules", {}).get("items", [])
    conventions = raw.get("conventions", {}).get("items", [])
    active = raw.get("active_task", {})

    lines = [f"# AGENTS.md — {name}", "", "## Project", ""]
    if purpose:
        lines.append(purpose)
    if stack:
        lines.append(f"\n**Stack:** {', '.join(stack)}")
    lines.append("")

    if decisions:
        lines += ["## Architecture", ""]
        for d in decisions:
            lines.append(f"### {d.get('what', '')}")
            lines.append(f"**Why:** {d.get('why', '')}")
            lines.append("")

    if constraints:
        lines += ["## Constraints", ""]
        for c in constraints:
            lines.append(f"- {c}")
        lines.append("")

    if zones:
        lines += ["## Danger Zones", ""]
        for z in zones:
            lines.append(f"- {z}")
        lines.append("")

    if modules:
        lines += ["## Modules", ""]
        for m in modules:
            owner = f" (owner: {m['owner']})" if m.get("owner") else ""
            lines.append(f"- `{m['name']}`: {m.get('description', '')}{owner}")
        lines.append("")

    if conventions:
        lines += ["## Conventions", ""]
        for c in conventions:
            lines.append(f"- {c}")
        lines.append("")

    if active.get("title"):
        lines += ["## Active Task", ""]
        lines.append(f"**{active['title']}**")
        if active.get("description"):
            lines.append(active["description"])
        lines.append("")

    output_path.write_text("\n".join(lines), encoding="utf-8")

    print(
        f"\n[smm-sync] Migrated smm.toml → AGENTS.md. "
        f"smm.toml is no longer used. You can delete it.\n"
    )
    return True
