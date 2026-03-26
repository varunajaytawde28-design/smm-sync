"""Tests for GitHub passive capture module.

All GitHub API and Anthropic API calls are mocked — no real network calls.
"""
from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import yaml

from smm_sync.capture.github_capture import (
    GitHubCapture,
    keyword_filter,
    load_capture_state,
    load_config,
    save_capture_state,
    extract_decision,
)
from smm_sync.capture.models import (
    CaptureSettings,
    CaptureTypes,
    CapturedEvent,
    GithubCaptureConfig,
    RepoConfig,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture()
def tmp_dir(tmp_path):
    """Return a temporary directory Path."""
    return tmp_path


@pytest.fixture()
def valid_github_yml(tmp_dir) -> Path:
    """Write a minimal valid github.yml and return its path."""
    config = {
        "repos": [
            {
                "owner": "testuser",
                "name": "test-repo",
                "project": "test-project",
                "capture": {
                    "pull_requests": True,
                    "commits": True,
                    "issues": False,
                    "releases": True,
                },
            }
        ],
        "settings": {
            "poll_interval_minutes": 15,
            "lookback_days": 7,
            "min_content_length": 30,
            "decision_keywords": ["decided", "chose", "because"],
        },
    }
    path = tmp_dir / "github.yml"
    path.write_text(yaml.dump(config), encoding="utf-8")
    return path


@pytest.fixture()
def state_path(tmp_dir) -> Path:
    """Return a path for capture_state.json that doesn't exist yet."""
    return tmp_dir / "capture_state.json"


@pytest.fixture()
def mock_graph_client():
    """Return a mock GraphClient that accepts add_decision calls."""
    client = MagicMock()
    client.add_decision = AsyncMock(return_value="fake-uuid")
    return client


# ---------------------------------------------------------------------------
# Config loading
# ---------------------------------------------------------------------------

class TestLoadConfig:
    def test_loads_valid_yaml(self, valid_github_yml):
        config = load_config(valid_github_yml)
        assert isinstance(config, GithubCaptureConfig)
        assert len(config.repos) == 1
        assert config.repos[0].owner == "testuser"
        assert config.repos[0].name == "test-repo"
        assert config.repos[0].project == "test-project"
        assert config.repos[0].capture.pull_requests is True
        assert config.repos[0].capture.issues is False
        assert config.settings.poll_interval_minutes == 15
        assert config.settings.lookback_days == 7

    def test_raises_if_missing(self, tmp_dir):
        with pytest.raises(FileNotFoundError):
            load_config(tmp_dir / "nonexistent.yml")

    def test_raises_on_missing_required_fields(self, tmp_dir):
        bad = tmp_dir / "bad.yml"
        bad.write_text(yaml.dump({"repos": [{"owner": "x"}]}))
        with pytest.raises(Exception):
            load_config(bad)

    def test_default_settings_applied(self, tmp_dir):
        minimal = {
            "repos": [
                {"owner": "a", "name": "b", "project": "c"}
            ]
        }
        path = tmp_dir / "minimal.yml"
        path.write_text(yaml.dump(minimal))
        config = load_config(path)
        assert config.settings.poll_interval_minutes == 30
        assert config.settings.lookback_days == 30
        assert len(config.settings.decision_keywords) > 0


# ---------------------------------------------------------------------------
# Capture state
# ---------------------------------------------------------------------------

class TestCaptureState:
    def test_returns_empty_dict_if_missing(self, state_path):
        result = load_capture_state(state_path)
        assert result == {}

    def test_loads_existing_state(self, state_path):
        data = {
            "testuser/test-repo": {
                "last_pr_number": 42,
                "last_commit_sha": "abc123",
                "last_run": "2026-03-22T18:00:00Z",
            }
        }
        state_path.write_text(json.dumps(data), encoding="utf-8")
        result = load_capture_state(state_path)
        assert result["testuser/test-repo"]["last_pr_number"] == 42

    def test_saves_and_reloads_state(self, state_path):
        data = {"myrepo": {"last_pr_number": 7, "last_commit_sha": "def456"}}
        save_capture_state(state_path, data)
        assert state_path.exists()
        reloaded = load_capture_state(state_path)
        assert reloaded["myrepo"]["last_pr_number"] == 7

    def test_atomic_write_creates_file(self, state_path):
        save_capture_state(state_path, {"test": {"last_pr_number": 1}})
        assert state_path.exists()
        # Ensure no temp files left behind
        assert not list(state_path.parent.glob("*.tmp"))

    def test_created_on_first_run_if_missing(self, state_path):
        assert not state_path.exists()
        save_capture_state(state_path, {})
        assert state_path.exists()


# ---------------------------------------------------------------------------
# Keyword filter
# ---------------------------------------------------------------------------

class TestKeywordFilter:
    def test_returns_true_for_matching_keyword(self):
        assert keyword_filter("We decided to use Postgres", ["decided", "chose"])

    def test_returns_true_case_insensitive(self):
        assert keyword_filter("WE DECIDED this approach", ["decided"])

    def test_returns_false_for_no_keywords(self):
        assert not keyword_filter("This is a routine fix", ["decided", "chose", "rejected"])

    def test_returns_false_for_empty_content(self):
        assert not keyword_filter("", ["decided"])

    def test_returns_false_for_empty_keywords(self):
        assert not keyword_filter("We decided something", [])

    def test_multi_word_keyword(self):
        assert keyword_filter("We chose this instead of the old approach", ["instead of"])

    def test_partial_match_does_not_trigger(self):
        # "choice" should not match keyword "chose"
        assert not keyword_filter("This was a good choice", ["chose"])


# ---------------------------------------------------------------------------
# extract_decision
# ---------------------------------------------------------------------------

class TestExtractDecision:
    @pytest.mark.asyncio
    async def test_returns_none_for_no_decision(self):
        with patch("smm_sync.capture.github_capture.anthropic") as mock_anthropic:
            mock_client = AsyncMock()
            mock_anthropic.AsyncAnthropic.return_value = mock_client
            mock_msg = MagicMock()
            mock_msg.content = [MagicMock(text="NO_DECISION")]
            mock_client.messages.create = AsyncMock(return_value=mock_msg)

            result = await extract_decision("Random text", "test source", "fake-key")
            assert result is None

    @pytest.mark.asyncio
    async def test_returns_extracted_sentence(self):
        with patch("smm_sync.capture.github_capture.anthropic") as mock_anthropic:
            mock_client = AsyncMock()
            mock_anthropic.AsyncAnthropic.return_value = mock_client
            mock_msg = MagicMock()
            mock_msg.content = [MagicMock(text="We decided to use Postgres for the database.")]
            mock_client.messages.create = AsyncMock(return_value=mock_msg)

            result = await extract_decision(
                "We decided to use Postgres because it supports JSONB.",
                "PR #1",
                "fake-key",
            )
            assert result == "We decided to use Postgres for the database."

    @pytest.mark.asyncio
    async def test_returns_none_on_api_error(self):
        with patch("smm_sync.capture.github_capture.anthropic") as mock_anthropic:
            mock_client = AsyncMock()
            mock_anthropic.AsyncAnthropic.return_value = mock_client
            mock_client.messages.create = AsyncMock(side_effect=Exception("API error"))

            result = await extract_decision("Some content", "source", "fake-key")
            assert result is None


# ---------------------------------------------------------------------------
# CapturedEvent model
# ---------------------------------------------------------------------------

class TestCapturedEvent:
    def test_valid_event(self):
        event = CapturedEvent(
            repo="owner/repo",
            event_type="pr",
            event_id="42",
            title="Add feature X",
            content="We decided to use approach Y because...",
            url="https://github.com/owner/repo/pull/42",
        )
        assert event.repo == "owner/repo"
        assert event.event_type == "pr"
        assert event.decision_extracted is None
        assert isinstance(event.captured_at, datetime)

    def test_event_with_decision(self):
        event = CapturedEvent(
            repo="owner/repo",
            event_type="commit",
            event_id="abc123",
            title="fix: use atomic writes",
            content="Use os.rename for atomic writes because it prevents corruption.",
            url="https://github.com/owner/repo/commit/abc123",
            decision_extracted="Use os.rename for atomic writes to prevent file corruption.",
        )
        assert event.decision_extracted is not None

    def test_event_type_values(self):
        for etype in ("pr", "commit", "issue", "release"):
            e = CapturedEvent(
                repo="a/b",
                event_type=etype,
                event_id="1",
                title="t",
                content="c",
                url="http://example.com",
            )
            assert e.event_type == etype


# ---------------------------------------------------------------------------
# Short content skipping
# ---------------------------------------------------------------------------

class TestShortContentSkipping:
    @pytest.mark.asyncio
    async def test_skip_pr_under_min_length(self, valid_github_yml, state_path, mock_graph_client):
        capture = GitHubCapture(
            config_path=valid_github_yml,
            state_path=state_path,
            graph_client=mock_graph_client,
            github_token="fake",
            api_key="fake",
        )
        repo_config = capture.config.repos[0]
        state = {}

        mock_pr = MagicMock()
        mock_pr.number = 1
        mock_pr.title = "Fix"
        mock_pr.body = "x"  # very short — below min_content_length=30
        mock_pr.html_url = "https://github.com/test/pr/1"
        mock_pr.user = MagicMock(login="someone")
        mock_pr.updated_at = datetime.now(timezone.utc)

        mock_repo = MagicMock()
        mock_repo.get_pulls.return_value = [mock_pr]

        with patch.object(capture, "_get_github") as mock_gh:
            mock_gh.return_value.get_repo.return_value = mock_repo
            count = await capture.capture_pull_requests(repo_config, state)

        assert count == 0
        mock_graph_client.add_decision.assert_not_called()
