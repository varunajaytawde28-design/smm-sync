"""Tests for smm onboard command."""
from __future__ import annotations

import pytest
from click.testing import CliRunner
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch


@pytest.fixture
def runner():
    """Return a Click test runner."""
    return CliRunner()


class TestOnboardCommand:
    """Tests for the smm onboard CLI command."""

    def test_onboard_requires_api_key(self, runner, tmp_path):
        """smm onboard exits with error if ANTHROPIC_API_KEY not set."""
        from smm_sync.cli import main

        with runner.isolated_filesystem(temp_dir=tmp_path):
            # Create .smm dir
            smm_dir = Path(".smm")
            smm_dir.mkdir()

            with patch.dict("os.environ", {}, clear=True):
                # Remove key if present
                import os
                os.environ.pop("ANTHROPIC_API_KEY", None)

                result = runner.invoke(main, ["onboard"])

        assert result.exit_code != 0 or "ANTHROPIC_API_KEY" in result.output

    def test_onboard_requires_smm_dir(self, runner, tmp_path):
        """smm onboard exits with error if .smm/ not found."""
        from smm_sync.cli import main

        with runner.isolated_filesystem(temp_dir=tmp_path):
            # Do NOT create .smm dir
            with patch.dict("os.environ", {"ANTHROPIC_API_KEY": "test-key"}):
                result = runner.invoke(main, ["onboard"])

        assert result.exit_code != 0 or "smm init" in result.output

    def test_generate_onboarding_doc_empty_graph(self):
        """_generate_onboarding_doc with empty graph returns graceful message."""
        import asyncio
        from smm_sync.cli import _generate_onboarding_doc
        from unittest.mock import MagicMock, AsyncMock

        mock_client = MagicMock()
        mock_client.get_decisions = AsyncMock(return_value=[])

        result = asyncio.run(
            _generate_onboarding_doc(mock_client, project="test-project", api_key="test-key")
        )

        assert isinstance(result, str)
        assert "test-project" in result
        # Should not crash on empty graph
        assert len(result) > 50

    def test_generate_onboarding_doc_contains_sections(self):
        """_generate_onboarding_doc output contains key sections."""
        import asyncio
        from smm_sync.cli import _generate_onboarding_doc
        from unittest.mock import MagicMock, AsyncMock, patch
        from smm_sync.context_graph.models import Decision
        from datetime import datetime

        mock_decisions = [
            Decision(
                id="d1",
                title="Use os.rename() for atomic locking",
                content="We use os.rename().\nRationale: POSIX atomic.",
                rationale="POSIX atomic",
                made_by="varun",
                project="test-project",
                created_at=datetime.utcnow(),
                valid=True,
            ),
            Decision(
                id="d2",
                title="[CONSTRAINT] Python 3.11+ only",
                content="[CONSTRAINT] Python 3.11+ only.\nRationale: tomllib stdlib.",
                rationale="tomllib stdlib",
                made_by="varun",
                project="test-project",
                created_at=datetime.utcnow(),
                valid=True,
            ),
        ]

        mock_client = MagicMock()
        mock_client.get_decisions = AsyncMock(return_value=mock_decisions)

        # Mock the Anthropic API call
        mock_anthropic_msg = MagicMock()
        mock_anthropic_msg.content = [MagicMock(text="## What this project does\n\nTest content.\n\n## Key decisions\n\n- Use os.rename()\n\n## Constraints\n\n- Python 3.11+\n")]

        with patch("anthropic.Anthropic") as MockAnthropicClass:
            mock_instance = MagicMock()
            mock_instance.messages.create.return_value = mock_anthropic_msg
            MockAnthropicClass.return_value = mock_instance

            result = asyncio.run(
                _generate_onboarding_doc(mock_client, project="test-project", api_key="test-key")
            )

        assert isinstance(result, str)
        assert "test-project" in result
        assert "Generated" in result

    def test_generate_onboarding_doc_handles_api_failure(self):
        """_generate_onboarding_doc uses template fallback when API fails."""
        import asyncio
        from smm_sync.cli import _generate_onboarding_doc
        from unittest.mock import MagicMock, AsyncMock, patch
        from smm_sync.context_graph.models import Decision
        from datetime import datetime

        mock_client = MagicMock()
        mock_client.get_decisions = AsyncMock(return_value=[])

        with patch("anthropic.Anthropic") as MockAnthropicClass:
            MockAnthropicClass.side_effect = Exception("API unavailable")

            result = asyncio.run(
                _generate_onboarding_doc(mock_client, project="test-project", api_key="test-key")
            )

        # Should return something even on failure
        assert isinstance(result, str)
        assert len(result) > 0
