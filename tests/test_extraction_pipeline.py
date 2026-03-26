"""Tests for the two-stage DRMiner-style extraction pipeline.

Research basis: DRMiner (ICSE 2024) — F1 0.65 vs 0.58 for raw LLM.
Two-stage hybrid pipeline with binary classifier (Stage 1) + structured
extractor (Stage 2) achieves 14x improvement in downstream task quality.

All Anthropic API calls are mocked — no real network calls.
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import yaml


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _run(coro):
    return asyncio.run(coro)


@pytest.fixture()
def valid_github_yml(tmp_path) -> Path:
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
    path = tmp_path / "github.yml"
    path.write_text(yaml.dump(config), encoding="utf-8")
    return path


@pytest.fixture()
def mock_graph_client():
    client = MagicMock()
    client.add_decision = AsyncMock(return_value="fake-uuid")
    client.search_context = AsyncMock(return_value=[])
    return client


@pytest.fixture()
def capture(valid_github_yml, mock_graph_client, tmp_path):
    from smm_sync.capture.github_capture import GitHubCapture

    return GitHubCapture(
        config_path=valid_github_yml,
        state_path=tmp_path / "capture_state.json",
        graph_client=mock_graph_client,
        github_token="fake-token",
        api_key="fake-api-key",
    )


# ---------------------------------------------------------------------------
# Stage 1: Binary classifier
# ---------------------------------------------------------------------------

class TestStage1Classifier:
    @pytest.mark.asyncio
    async def test_returns_none_for_bug_fix_text(self, capture):
        """Stage 1 should return None for non-decision content (bug fixes)."""
        bug_fix_text = "Fix typo in README. Change 'teh' to 'the'."

        with patch.object(capture, "_call_llm", new=AsyncMock(return_value="NO")) as mock_llm:
            result = await capture.extract_decision_two_stage(bug_fix_text, "commit abc123")

        assert result is None
        # Stage 1 called once; Stage 2 never called
        mock_llm.assert_called_once()
        call_args = mock_llm.call_args
        assert call_args.kwargs.get("model") == "claude-haiku-4-5-20251001"

    @pytest.mark.asyncio
    async def test_returns_result_for_decision_text(self, capture):
        """Stage 1 should pass decision text through to Stage 2."""
        decision_text = (
            "We decided to use os.rename() for atomic file locking because "
            "it is POSIX-atomic on macOS and Linux without any external dependencies. "
            "We rejected Redis locks (network dependency) and SQLite (too heavy)."
        )
        stage2_json = json.dumps({
            "chosen_decision": "Use os.rename() for atomic file locking.",
            "rejected_alternatives": ["Redis locks", "SQLite"],
            "contextual_arguments": "POSIX-atomic on macOS/Linux, no external deps.",
            "confidence": 0.92,
        })

        call_count = 0

        async def mock_llm(prompt, model, max_tokens):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return "YES"  # Stage 1
            return stage2_json  # Stage 2

        with patch.object(capture, "_call_llm", side_effect=mock_llm):
            result = await capture.extract_decision_two_stage(decision_text, "PR #42")

        assert result is not None
        assert result["chosen_decision"] == "Use os.rename() for atomic file locking."
        assert "Redis locks" in result["rejected_alternatives"]
        assert result["confidence"] == 0.92
        assert call_count == 2  # Both stages called

    @pytest.mark.asyncio
    async def test_stage1_uses_haiku_model(self, capture):
        """Stage 1 must use claude-haiku-4-5-20251001 (cheap binary filter)."""
        with patch.object(capture, "_call_llm", new=AsyncMock(return_value="NO")) as mock_llm:
            await capture.extract_decision_two_stage("some text", "source")

        mock_llm.assert_called_once()
        assert mock_llm.call_args.kwargs["model"] == "claude-haiku-4-5-20251001"

    @pytest.mark.asyncio
    async def test_stage1_returns_none_for_formatting_change(self, capture):
        """Formatting changes should not pass Stage 1."""
        with patch.object(capture, "_call_llm", new=AsyncMock(return_value="NO")):
            result = await capture.extract_decision_two_stage(
                "Fix indentation in config.py", "commit"
            )
        assert result is None


# ---------------------------------------------------------------------------
# Stage 2: Structured extractor
# ---------------------------------------------------------------------------

class TestStage2Extractor:
    @pytest.mark.asyncio
    async def test_stage2_uses_sonnet_model(self, capture):
        """Stage 2 must use claude-sonnet-4-6 (structured extractor)."""
        stage2_json = json.dumps({
            "chosen_decision": "Use X.",
            "rejected_alternatives": [],
            "contextual_arguments": "Because Y.",
            "confidence": 0.80,
        })

        models_used = []

        async def mock_llm(prompt, model, max_tokens):
            models_used.append(model)
            if len(models_used) == 1:
                return "YES"
            return stage2_json

        with patch.object(capture, "_call_llm", side_effect=mock_llm):
            await capture.extract_decision_two_stage("We decided to use X because Y.", "PR")

        assert models_used[0] == "claude-haiku-4-5-20251001"
        assert models_used[1] == "claude-sonnet-4-6"

    @pytest.mark.asyncio
    async def test_stage2_returns_none_for_low_confidence(self, capture):
        """Stage 2 should return None when confidence < 0.50."""
        low_conf_json = json.dumps({
            "chosen_decision": "Maybe use X.",
            "rejected_alternatives": [],
            "contextual_arguments": "Unclear.",
            "confidence": 0.40,
        })

        async def mock_llm(prompt, model, max_tokens):
            if "YES" in prompt or max_tokens == 5:
                return "YES"
            return low_conf_json

        with patch.object(capture, "_call_llm", side_effect=mock_llm):
            result = await capture.extract_decision_two_stage("Some text", "source")

        assert result is None

    @pytest.mark.asyncio
    async def test_stage2_returns_none_for_invalid_json(self, capture):
        """Stage 2 should return None gracefully when JSON parse fails."""
        async def mock_llm(prompt, model, max_tokens):
            if max_tokens == 5:
                return "YES"
            return "this is not valid json at all {{{"

        with patch.object(capture, "_call_llm", side_effect=mock_llm):
            result = await capture.extract_decision_two_stage("Some text", "source")

        assert result is None

    @pytest.mark.asyncio
    async def test_stage2_extracts_correct_schema(self, capture):
        """Stage 2 result must have the required keys."""
        stage2_json = json.dumps({
            "chosen_decision": "Use Kuzu as embedded graph DB.",
            "rejected_alternatives": ["FalkorDB via Docker", "Neo4j Aura"],
            "contextual_arguments": "macOS blocks Docker; Kuzu runs in-process.",
            "confidence": 0.88,
        })

        async def mock_llm(prompt, model, max_tokens):
            if max_tokens == 5:
                return "YES"
            return stage2_json

        with patch.object(capture, "_call_llm", side_effect=mock_llm):
            result = await capture.extract_decision_two_stage("We use Kuzu because...", "PR #5")

        assert result is not None
        assert "chosen_decision" in result
        assert "rejected_alternatives" in result
        assert "contextual_arguments" in result
        assert "confidence" in result
        assert isinstance(result["rejected_alternatives"], list)


# ---------------------------------------------------------------------------
# Two-stage pipeline: Sonnet only called when Stage 1 passes
# ---------------------------------------------------------------------------

class TestTwoStagePipelineOrdering:
    @pytest.mark.asyncio
    async def test_stage2_not_called_when_stage1_fails(self, capture):
        """Sonnet (Stage 2) must NOT be called when Haiku (Stage 1) says NO."""
        call_log = []

        async def mock_llm(prompt, model, max_tokens):
            call_log.append(model)
            return "NO"  # Stage 1 always says NO

        with patch.object(capture, "_call_llm", side_effect=mock_llm):
            result = await capture.extract_decision_two_stage("Just a typo fix", "commit")

        assert result is None
        assert len(call_log) == 1
        assert "sonnet" not in call_log[0]

    @pytest.mark.asyncio
    async def test_stage2_called_only_when_stage1_passes(self, capture):
        """Sonnet must be called EXACTLY ONCE when Haiku says YES."""
        call_log = []
        stage2_json = json.dumps({
            "chosen_decision": "Use X.",
            "rejected_alternatives": [],
            "contextual_arguments": "Because.",
            "confidence": 0.75,
        })

        async def mock_llm(prompt, model, max_tokens):
            call_log.append(model)
            if len(call_log) == 1:
                return "YES"
            return stage2_json

        with patch.object(capture, "_call_llm", side_effect=mock_llm):
            await capture.extract_decision_two_stage("We decided X because Y.", "PR #1")

        assert len(call_log) == 2
        assert call_log[0] == "claude-haiku-4-5-20251001"
        assert call_log[1] == "claude-sonnet-4-6"

    @pytest.mark.asyncio
    async def test_stage1_failure_returns_none_gracefully(self, capture):
        """Stage 1 API failure should return None, not raise."""
        with patch.object(capture, "_call_llm", new=AsyncMock(side_effect=Exception("network error"))):
            result = await capture.extract_decision_two_stage("some text", "source")

        assert result is None
