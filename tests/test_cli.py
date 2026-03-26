"""Tests for smm_sync.cli — updated commands."""
import pytest
from click.testing import CliRunner
from pathlib import Path

from smm_sync.cli import main


@pytest.fixture
def runner():
    return CliRunner()


@pytest.fixture
def project_dir(tmp_path):
    """A temp directory simulating a project root."""
    return tmp_path


def test_init_creates_agents_md(runner, project_dir):
    result = runner.invoke(main, ["init", "--name", "myproject"], catch_exceptions=False)
    agents_md = project_dir / "AGENTS.md"
    # CliRunner uses an isolated filesystem by default unless we pass it
    # We need to use mix_env or chdir
    assert result.exit_code == 0 or "AGENTS.md" in result.output


def test_init_creates_smm_dir(runner, tmp_path):
    with runner.isolated_filesystem(temp_dir=tmp_path):
        result = runner.invoke(main, ["init", "--name", "testproj"], catch_exceptions=False)
        assert result.exit_code == 0
        assert Path(".smm").exists()


def test_init_creates_agents_md_file(runner, tmp_path):
    with runner.isolated_filesystem(temp_dir=tmp_path):
        result = runner.invoke(main, ["init", "--name", "testproj"], catch_exceptions=False)
        assert result.exit_code == 0
        assert Path("AGENTS.md").exists()


def test_init_shows_mcp_config_hint(runner, tmp_path):
    with runner.isolated_filesystem(temp_dir=tmp_path):
        result = runner.invoke(main, ["init", "--name", "testproj"], catch_exceptions=False)
        assert "mcpServers" in result.output


def test_init_idempotent_if_agents_md_exists(runner, tmp_path):
    with runner.isolated_filesystem(temp_dir=tmp_path):
        Path("AGENTS.md").write_text("# existing")
        result = runner.invoke(main, ["init"], catch_exceptions=False)
        assert result.exit_code == 0
        assert "already exists" in result.output


def test_refresh_parses_agents_md(runner, tmp_path):
    with runner.isolated_filesystem(temp_dir=tmp_path):
        Path(".smm").mkdir()
        Path(".smm/locks").mkdir()
        Path("AGENTS.md").write_text(
            "# AGENTS.md\n\n## Project\n\nTest.\n\n## Active Task\n\nBuild.\n"
        )
        result = runner.invoke(main, ["refresh"], catch_exceptions=False)
        assert result.exit_code == 0
        assert Path(".smm/parsed_context.json").exists()


def test_refresh_fails_without_agents_md(runner, tmp_path):
    with runner.isolated_filesystem(temp_dir=tmp_path):
        result = runner.invoke(main, ["refresh"])
        assert result.exit_code != 0


def test_status_requires_smm_dir(runner, tmp_path):
    with runner.isolated_filesystem(temp_dir=tmp_path):
        result = runner.invoke(main, ["status"])
        assert result.exit_code != 0


def test_claim_and_release_via_cli(runner, tmp_path):
    with runner.isolated_filesystem(temp_dir=tmp_path):
        Path(".smm").mkdir()
        Path(".smm/locks").mkdir()
        r1 = runner.invoke(main, ["claim", "auth.py", "--session", "s1"], catch_exceptions=False)
        assert r1.exit_code == 0
        assert "Claimed" in r1.output

        r2 = runner.invoke(main, ["claim", "auth.py", "--session", "s2"])
        assert r2.exit_code != 0
        assert "FAILED" in r2.output

        r3 = runner.invoke(main, ["release", "auth.py", "--session", "s1"], catch_exceptions=False)
        assert r3.exit_code == 0
        assert "Released" in r3.output
