"""Tests for smm_sync.mcp_server — 4 MCP tools."""
import pytest
from pathlib import Path

import smm_sync.mcp_server as mcp_module
from smm_sync import state
from smm_sync import coordinator


@pytest.fixture
def smm_dir(tmp_path):
    d = tmp_path / ".smm"
    d.mkdir()
    (d / "locks").mkdir()
    return d


@pytest.fixture
def agents_md(tmp_path):
    p = tmp_path / "AGENTS.md"
    p.write_text(
        "# AGENTS.md\n\n## Project\n\nTest project.\n\n## Active Task\n\nBuild tests.\n"
    )
    return p


@pytest.fixture(autouse=True)
def configure_server(smm_dir):
    """Point the MCP module at the tmp smm_dir for each test."""
    old_context_loaded = mcp_module._context_loaded
    mcp_module._smm_dir = smm_dir
    mcp_module._context_loaded = True  # bypass session gate in unit tests
    yield
    mcp_module._smm_dir = None
    mcp_module._context_loaded = old_context_loaded


# ---------------------------------------------------------------------------
# read_context
# ---------------------------------------------------------------------------

def test_read_context_returns_string(agents_md):
    mcp_module._smm_dir = agents_md.parent / ".smm"
    mcp_module._smm_dir.mkdir(exist_ok=True)
    (mcp_module._smm_dir / "locks").mkdir(exist_ok=True)
    result = mcp_module.read_context()
    assert isinstance(result, str)
    assert len(result) > 0


def test_read_context_includes_agents_md_content(agents_md):
    mcp_module._smm_dir = agents_md.parent / ".smm"
    mcp_module._smm_dir.mkdir(exist_ok=True)
    (mcp_module._smm_dir / "locks").mkdir(exist_ok=True)
    result = mcp_module.read_context()
    assert "Test project" in result


def test_read_context_includes_coordination_state(smm_dir):
    result = mcp_module.read_context()
    assert "Coordination State" in result


# ---------------------------------------------------------------------------
# claim_file
# ---------------------------------------------------------------------------

def test_claim_file_succeeds(smm_dir):
    result = mcp_module.claim_file("auth.py", "agent-1")
    assert result["success"] is True


def test_claim_file_fails_if_already_claimed_by_other(smm_dir):
    mcp_module.claim_file("auth.py", "agent-1")
    result = mcp_module.claim_file("auth.py", "agent-2")
    assert result["success"] is False
    assert "conflict" in result
    assert "agent-1" in result["conflict"]


def test_claim_file_stores_task(smm_dir):
    mcp_module.claim_file("auth.py", "agent-1", task="refactor auth")
    s = state.get_current_state(smm_dir)
    assert s["claimed_files"]["auth.py"]["task"] == "refactor auth"


def test_claim_file_different_files_independently(smm_dir):
    r1 = mcp_module.claim_file("auth.py", "agent-1")
    r2 = mcp_module.claim_file("db.py", "agent-2")
    assert r1["success"] is True
    assert r2["success"] is True


# ---------------------------------------------------------------------------
# release_file
# ---------------------------------------------------------------------------

def test_release_file_succeeds(smm_dir):
    mcp_module.claim_file("auth.py", "agent-1")
    result = mcp_module.release_file("auth.py", "agent-1")
    assert result["success"] is True


def test_release_file_fails_if_not_claimed(smm_dir):
    result = mcp_module.release_file("auth.py", "agent-1")
    assert result["success"] is False
    assert "reason" in result


def test_release_file_fails_if_owned_by_other(smm_dir):
    mcp_module.claim_file("auth.py", "agent-1")
    result = mcp_module.release_file("auth.py", "agent-2")
    assert result["success"] is False


def test_after_release_file_can_be_reclaimed(smm_dir):
    mcp_module.claim_file("auth.py", "agent-1")
    mcp_module.release_file("auth.py", "agent-1")
    result = mcp_module.claim_file("auth.py", "agent-2")
    assert result["success"] is True


# ---------------------------------------------------------------------------
# refresh_context
# ---------------------------------------------------------------------------

def test_refresh_context_accepted_when_new(agents_md):
    mcp_module._smm_dir = agents_md.parent / ".smm"
    mcp_module._smm_dir.mkdir(exist_ok=True)
    (mcp_module._smm_dir / "locks").mkdir(exist_ok=True)
    result = mcp_module.refresh_context("agent-1")
    assert result["changed"] is True
    assert "context" in result


def test_refresh_context_rejected_when_unchanged(agents_md):
    mcp_module._smm_dir = agents_md.parent / ".smm"
    mcp_module._smm_dir.mkdir(exist_ok=True)
    (mcp_module._smm_dir / "locks").mkdir(exist_ok=True)
    mcp_module.refresh_context("agent-1")
    result = mcp_module.refresh_context("agent-1")
    assert result["changed"] is False


def test_refresh_context_no_agents_md(smm_dir):
    result = mcp_module.refresh_context("agent-1")
    assert result["changed"] is False
    assert "not found" in result["reason"]
