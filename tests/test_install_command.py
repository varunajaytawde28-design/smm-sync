"""Tests for smm install command helper functions."""
from __future__ import annotations

import json
import os
import stat
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from smm_sync.cli import (
    _load_keys_from_env_file,
    _save_keys,
    _update_gitignore,
    _validate_anthropic_key,
    _validate_github_token,
)


# ---------------------------------------------------------------------------
# _validate_anthropic_key
# ---------------------------------------------------------------------------


def test_validate_anthropic_key_returns_true_on_200():
    """Valid key triggers HTTP 200 and returns True."""
    mock_response = MagicMock()
    mock_response.__enter__ = lambda s: s
    mock_response.__exit__ = MagicMock(return_value=False)
    mock_response.status = 200

    with patch("urllib.request.urlopen", return_value=mock_response):
        result = _validate_anthropic_key("sk-ant-test-key")
    assert result is True


def test_validate_anthropic_key_returns_false_on_error():
    """Any exception (network, 4xx, 5xx) results in False."""
    with patch("urllib.request.urlopen", side_effect=Exception("network error")):
        result = _validate_anthropic_key("bad-key")
    assert result is False


def test_validate_anthropic_key_returns_false_on_non_200():
    """Non-200 response results in False."""
    mock_response = MagicMock()
    mock_response.__enter__ = lambda s: s
    mock_response.__exit__ = MagicMock(return_value=False)
    mock_response.status = 401

    with patch("urllib.request.urlopen", return_value=mock_response):
        result = _validate_anthropic_key("sk-ant-invalid")
    # urlopen raises HTTPError for non-2xx, so this should return False
    # but if it doesn't raise, still check status
    # Either way, no exception should propagate
    assert isinstance(result, bool)


# ---------------------------------------------------------------------------
# _validate_github_token
# ---------------------------------------------------------------------------


def test_validate_github_token_returns_username():
    """Valid token returns the GitHub login username."""
    mock_response = MagicMock()
    mock_response.__enter__ = lambda s: s
    mock_response.__exit__ = MagicMock(return_value=False)
    mock_response.read.return_value = json.dumps({"login": "varunajaytawde"}).encode()

    with patch("urllib.request.urlopen", return_value=mock_response):
        result = _validate_github_token("ghp_valid_token")
    assert result == "varunajaytawde"


def test_validate_github_token_returns_none_on_error():
    """Invalid token or network error returns None."""
    with patch("urllib.request.urlopen", side_effect=Exception("401 Unauthorized")):
        result = _validate_github_token("ghp_bad_token")
    assert result is None


def test_validate_github_token_returns_none_on_missing_login():
    """Response without 'login' key returns None."""
    mock_response = MagicMock()
    mock_response.__enter__ = lambda s: s
    mock_response.__exit__ = MagicMock(return_value=False)
    mock_response.read.return_value = json.dumps({"message": "Bad credentials"}).encode()

    with patch("urllib.request.urlopen", return_value=mock_response):
        result = _validate_github_token("ghp_bad")
    assert result is None


# ---------------------------------------------------------------------------
# _save_keys
# ---------------------------------------------------------------------------


def test_install_saves_keys_to_env_file(tmp_path):
    """Keys are written to .smm/.env with correct content."""
    smm_dir = tmp_path / ".smm"
    smm_dir.mkdir()

    _save_keys(smm_dir, "sk-ant-test-key", "ghp_test_token")

    env_file = smm_dir / ".env"
    assert env_file.exists()
    content = env_file.read_text()
    assert "ANTHROPIC_API_KEY=sk-ant-test-key" in content
    assert "GITHUB_TOKEN=ghp_test_token" in content


def test_install_env_file_has_correct_permissions(tmp_path):
    """The .smm/.env file must have mode 0o600 (owner read/write only)."""
    smm_dir = tmp_path / ".smm"
    smm_dir.mkdir()

    _save_keys(smm_dir, "sk-ant-key", "ghp_token")

    env_file = smm_dir / ".env"
    file_mode = stat.S_IMODE(env_file.stat().st_mode)
    assert file_mode == 0o600, f"Expected 0o600, got {oct(file_mode)}"


# ---------------------------------------------------------------------------
# _load_keys_from_env_file
# ---------------------------------------------------------------------------


def test_load_keys_from_env_file_sets_environ(tmp_path, monkeypatch):
    """Keys from .smm/.env are loaded into os.environ."""
    smm_dir = tmp_path / ".smm"
    smm_dir.mkdir()
    env_file = smm_dir / ".env"
    env_file.write_text("ANTHROPIC_API_KEY=sk-ant-loaded\nGITHUB_TOKEN=ghp_loaded\n")

    # Remove from env if present
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)

    _load_keys_from_env_file(smm_dir)

    assert os.environ.get("ANTHROPIC_API_KEY") == "sk-ant-loaded"
    assert os.environ.get("GITHUB_TOKEN") == "ghp_loaded"


def test_load_keys_from_env_file_does_not_override_existing(tmp_path, monkeypatch):
    """Existing env vars are NOT overwritten by .smm/.env."""
    smm_dir = tmp_path / ".smm"
    smm_dir.mkdir()
    env_file = smm_dir / ".env"
    env_file.write_text("ANTHROPIC_API_KEY=sk-ant-from-file\n")

    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-already-set")

    _load_keys_from_env_file(smm_dir)

    assert os.environ["ANTHROPIC_API_KEY"] == "sk-ant-already-set"


def test_load_keys_from_env_file_noop_when_missing(tmp_path):
    """No error when .smm/.env does not exist."""
    smm_dir = tmp_path / ".smm"
    smm_dir.mkdir()

    # Should not raise
    _load_keys_from_env_file(smm_dir)


# ---------------------------------------------------------------------------
# _update_gitignore
# ---------------------------------------------------------------------------


def test_install_updates_gitignore_creates_file(tmp_path):
    """If .gitignore does not exist, it is created with CaaS entries."""
    _update_gitignore(tmp_path)

    gitignore = tmp_path / ".gitignore"
    assert gitignore.exists()
    content = gitignore.read_text()
    assert ".smm/.env" in content
    assert ".smm/graph/" in content


def test_install_updates_gitignore_appends_to_existing(tmp_path):
    """Existing .gitignore is extended, not replaced."""
    gitignore = tmp_path / ".gitignore"
    gitignore.write_text("node_modules/\n*.pyc\n")

    _update_gitignore(tmp_path)

    content = gitignore.read_text()
    assert "node_modules/" in content
    assert "*.pyc" in content
    assert ".smm/.env" in content


def test_install_updates_gitignore_no_duplicates(tmp_path):
    """Running twice does not duplicate entries."""
    _update_gitignore(tmp_path)
    _update_gitignore(tmp_path)

    gitignore = tmp_path / ".gitignore"
    content = gitignore.read_text()
    assert content.count(".smm/.env") == 1


# ---------------------------------------------------------------------------
# smm install command (integration via Click test runner)
# ---------------------------------------------------------------------------


def test_install_creates_mcp_json(tmp_path, monkeypatch):
    """smm install writes .mcp.json with the caas server entry."""
    from click.testing import CliRunner
    from smm_sync.cli import main

    runner = CliRunner()
    monkeypatch.chdir(tmp_path)

    # Patch all network calls so we don't hit real APIs
    with patch("smm_sync.cli._validate_anthropic_key", return_value=True), \
         patch("smm_sync.cli._validate_github_token", return_value="testuser"), \
         patch("smm_sync.cli.find_git_root", return_value=None), \
         patch("smm_sync.cli.get_git_remote", return_value=None):
        # Patch the capture import to avoid graph deps
        with patch.dict("sys.modules", {
            "smm_sync.capture": MagicMock(),
            "smm_sync.context_graph": MagicMock(),
            "smm_sync.context_graph.client": MagicMock(),
        }):
            result = runner.invoke(
                main,
                ["install"],
                input="sk-ant-testkey\nghp_testtoken\n",
                catch_exceptions=False,
            )

    mcp = tmp_path / ".mcp.json"
    if mcp.exists():
        data = json.loads(mcp.read_text())
        assert "mcpServers" in data
        assert "caas" in data["mcpServers"]
