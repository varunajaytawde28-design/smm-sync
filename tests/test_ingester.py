"""Tests for smm_sync.ingester."""
import pytest
from pathlib import Path

from smm_sync.ingester import parse_agents_md, ingest, load_parsed_context


_SAMPLE_AGENTS_MD = """\
# AGENTS.md — test-project

## Project

test-project: A project for testing.

**Stack:** python, click

## Architecture

### Use pytest
**Why:** Industry standard.

## Constraints

- Python 3.11+ only

## Danger Zones

- Do not touch the database schema

## Modules

- `cli.py`: Entry point

## Conventions

- All functions have docstrings

## Active Task

**Build the ingester.**

Parse AGENTS.md into structured state.
"""


def test_parse_returns_all_sections():
    result = parse_agents_md(_SAMPLE_AGENTS_MD)
    assert result["project"]
    assert result["architecture"]
    assert result["constraints"]
    assert result["danger_zones"]
    assert result["modules"]
    assert result["conventions"]
    assert result["active_task"]


def test_parse_project_section_content():
    result = parse_agents_md(_SAMPLE_AGENTS_MD)
    assert "test-project" in result["project"]


def test_parse_constraints_content():
    result = parse_agents_md(_SAMPLE_AGENTS_MD)
    assert "Python 3.11+" in result["constraints"]


def test_parse_active_task_content():
    result = parse_agents_md(_SAMPLE_AGENTS_MD)
    assert "ingester" in result["active_task"]


def test_parse_produces_content_hash():
    result = parse_agents_md(_SAMPLE_AGENTS_MD)
    assert "content_hash" in result
    assert len(result["content_hash"]) == 64  # SHA256 hex


def test_parse_empty_string():
    result = parse_agents_md("")
    assert result["project"] == ""
    assert result["active_task"] == ""
    assert "content_hash" in result


def test_parse_minimal_file():
    content = "# AGENTS.md\n\n## Project\n\nJust a project.\n"
    result = parse_agents_md(content)
    assert "Just a project" in result["project"]


def test_parse_missing_sections_are_empty_strings():
    result = parse_agents_md("# AGENTS.md\n\n## Project\n\nOnly project.\n")
    assert result["architecture"] == ""
    assert result["active_task"] == ""


def test_ingest_writes_parsed_context_json(tmp_path):
    smm_dir = tmp_path / ".smm"
    smm_dir.mkdir()
    agents_md = tmp_path / "AGENTS.md"
    agents_md.write_text(_SAMPLE_AGENTS_MD)

    ingest(smm_dir, agents_md)

    assert (smm_dir / "parsed_context.json").exists()


def test_ingest_raises_if_agents_md_missing(tmp_path):
    smm_dir = tmp_path / ".smm"
    smm_dir.mkdir()
    with pytest.raises(FileNotFoundError):
        ingest(smm_dir, tmp_path / "AGENTS.md")


def test_load_parsed_context_returns_empty_dict_if_missing(tmp_path):
    smm_dir = tmp_path / ".smm"
    smm_dir.mkdir()
    assert load_parsed_context(smm_dir) == {}


def test_load_parsed_context_returns_written_content(tmp_path):
    smm_dir = tmp_path / ".smm"
    smm_dir.mkdir()
    agents_md = tmp_path / "AGENTS.md"
    agents_md.write_text(_SAMPLE_AGENTS_MD)

    ingest(smm_dir, agents_md)
    loaded = load_parsed_context(smm_dir)
    assert loaded["project"] != ""
    assert "content_hash" in loaded
