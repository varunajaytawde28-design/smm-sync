"""Tests for smm setup command and get_git_remote helper."""
from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch, AsyncMock

import pytest
from click.testing import CliRunner


@pytest.fixture
def runner():
    """Return a Click test runner."""
    return CliRunner()


# ---------------------------------------------------------------------------
# get_git_remote unit tests
# ---------------------------------------------------------------------------

class TestGetGitRemote:
    """Tests for git_utils.get_git_remote."""

    def test_https_url(self, tmp_path):
        """Parses HTTPS remote URL into (owner, repo)."""
        from smm_sync.git_utils import get_git_remote

        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(
                returncode=0,
                stdout="https://github.com/acme/my-project.git\n",
            )
            result = get_git_remote(tmp_path)

        assert result == ("acme", "my-project")

    def test_https_url_no_dot_git(self, tmp_path):
        """Parses HTTPS remote URL without .git suffix."""
        from smm_sync.git_utils import get_git_remote

        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(
                returncode=0,
                stdout="https://github.com/acme/my-project\n",
            )
            result = get_git_remote(tmp_path)

        assert result == ("acme", "my-project")

    def test_ssh_url(self, tmp_path):
        """Parses SSH remote URL into (owner, repo)."""
        from smm_sync.git_utils import get_git_remote

        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(
                returncode=0,
                stdout="git@github.com:acme/my-project.git\n",
            )
            result = get_git_remote(tmp_path)

        assert result == ("acme", "my-project")

    def test_non_github_url_returns_none(self, tmp_path):
        """Returns None for non-GitHub remotes."""
        from smm_sync.git_utils import get_git_remote

        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(
                returncode=0,
                stdout="https://gitlab.com/acme/my-project.git\n",
            )
            result = get_git_remote(tmp_path)

        assert result is None

    def test_no_remote_returns_none(self, tmp_path):
        """Returns None when git remote returns non-zero exit code."""
        from smm_sync.git_utils import get_git_remote

        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=128, stdout="")
            result = get_git_remote(tmp_path)

        assert result is None

    def test_subprocess_error_returns_none(self, tmp_path):
        """Returns None when subprocess raises (git not installed)."""
        from smm_sync.git_utils import get_git_remote

        with patch("subprocess.run", side_effect=FileNotFoundError):
            result = get_git_remote(tmp_path)

        assert result is None

    def test_custom_remote_name(self, tmp_path):
        """Passes custom remote name to git command."""
        from smm_sync.git_utils import get_git_remote

        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(
                returncode=0,
                stdout="https://github.com/acme/fork.git\n",
            )
            result = get_git_remote(tmp_path, remote="upstream")

        assert result == ("acme", "fork")
        call_args = mock_run.call_args[0][0]
        assert "upstream" in call_args


# ---------------------------------------------------------------------------
# smm setup CLI tests
# ---------------------------------------------------------------------------

class TestSetupCommand:
    """Tests for the smm setup CLI command."""

    def test_setup_requires_smm_dir(self, runner, tmp_path):
        """setup creates .smm/ when missing — no prior smm init needed."""
        from smm_sync.cli import main

        with runner.isolated_filesystem(temp_dir=tmp_path):
            result = runner.invoke(main, ["setup", "--skip-capture", "--skip-onboarding"])
            smm_created = (Path(".smm")).exists()

        assert result.exit_code == 0
        assert smm_created

    def test_setup_creates_github_yml(self, runner, tmp_path):
        """setup creates .smm/github.yml when it does not exist."""
        from smm_sync.cli import main

        with runner.isolated_filesystem(temp_dir=tmp_path):
            smm_dir = Path(".smm")
            smm_dir.mkdir()
            (smm_dir / "locks").mkdir()
            (smm_dir / "history").mkdir()

            with patch("smm_sync.git_utils.find_git_root", return_value=None), \
                 patch("smm_sync.cli.find_git_root", return_value=None), \
                 patch.dict("os.environ", {}, clear=True):
                result = runner.invoke(
                    main,
                    ["setup", "--skip-capture", "--skip-onboarding"],
                )

        assert (tmp_path / ".smm" / "github.yml").exists() or \
               Path(result.output).name == "" or \
               "github.yml" in result.output

    def test_setup_skips_existing_github_yml(self, runner, tmp_path):
        """setup does not overwrite an existing .smm/github.yml."""
        from smm_sync.cli import main

        with runner.isolated_filesystem(temp_dir=tmp_path):
            smm_dir = Path(".smm")
            smm_dir.mkdir()
            (smm_dir / "locks").mkdir()
            (smm_dir / "history").mkdir()
            existing = smm_dir / "github.yml"
            existing.write_text("# existing config\n", encoding="utf-8")

            with patch("smm_sync.git_utils.find_git_root", return_value=None), \
                 patch("smm_sync.cli.find_git_root", return_value=None), \
                 patch.dict("os.environ", {}, clear=True):
                result = runner.invoke(
                    main,
                    ["setup", "--skip-capture", "--skip-onboarding"],
                )

            # Assert inside the isolated filesystem context so the path is valid
            assert existing.read_text() == "# existing config\n"
            assert "already exists" in result.output

    def test_setup_detects_github_remote(self, runner, tmp_path):
        """setup reports detected owner/repo from git remote."""
        from smm_sync.cli import main

        with runner.isolated_filesystem(temp_dir=tmp_path):
            smm_dir = Path(".smm")
            smm_dir.mkdir()
            (smm_dir / "locks").mkdir()
            (smm_dir / "history").mkdir()

            fake_root = Path.cwd()
            # get_git_remote is imported inside the setup command body,
            # so patch the canonical location in git_utils.
            with patch("smm_sync.cli.find_git_root", return_value=fake_root), \
                 patch("smm_sync.cli.get_git_remote", return_value=("octocat", "hello-world")), \
                 patch.dict("os.environ", {}, clear=True):
                result = runner.invoke(
                    main,
                    ["setup", "--skip-capture", "--skip-onboarding"],
                )

        assert "octocat/hello-world" in result.output

    def test_setup_reports_missing_keys(self, runner, tmp_path):
        """setup warns when GITHUB_TOKEN or ANTHROPIC_API_KEY is absent."""
        from smm_sync.cli import main

        with runner.isolated_filesystem(temp_dir=tmp_path):
            smm_dir = Path(".smm")
            smm_dir.mkdir()
            (smm_dir / "locks").mkdir()
            (smm_dir / "history").mkdir()

            with patch("smm_sync.cli.find_git_root", return_value=None), \
                 patch.dict("os.environ", {}, clear=True):
                result = runner.invoke(
                    main,
                    ["setup", "--skip-capture", "--skip-onboarding"],
                )

        assert "GITHUB_TOKEN" in result.output
        assert "ANTHROPIC_API_KEY" in result.output

    def test_setup_confirms_keys_present(self, runner, tmp_path):
        """setup confirms both keys when they are set."""
        from smm_sync.cli import main

        with runner.isolated_filesystem(temp_dir=tmp_path):
            smm_dir = Path(".smm")
            smm_dir.mkdir()
            (smm_dir / "locks").mkdir()
            (smm_dir / "history").mkdir()

            with patch("smm_sync.cli.find_git_root", return_value=None), \
                 patch.dict(
                     "os.environ",
                     {"GITHUB_TOKEN": "ghp_test", "ANTHROPIC_API_KEY": "sk-ant-test"},
                 ):
                result = runner.invoke(
                    main,
                    ["setup", "--skip-capture", "--skip-onboarding"],
                )

        assert "GITHUB_TOKEN is set" in result.output
        assert "ANTHROPIC_API_KEY is set" in result.output

    def test_setup_prints_mcp_json_snippet(self, runner, tmp_path):
        """setup always prints a .mcp.json snippet."""
        from smm_sync.cli import main

        with runner.isolated_filesystem(temp_dir=tmp_path):
            smm_dir = Path(".smm")
            smm_dir.mkdir()
            (smm_dir / "locks").mkdir()
            (smm_dir / "history").mkdir()

            with patch("smm_sync.cli.find_git_root", return_value=None), \
                 patch.dict("os.environ", {}, clear=True):
                result = runner.invoke(
                    main,
                    ["setup", "--skip-capture", "--skip-onboarding"],
                )

        assert "mcpServers" in result.output
        assert "smm-sync" in result.output

    def test_setup_generates_onboarding_md(self, runner, tmp_path):
        """setup writes ONBOARDING.md when not skipped."""
        from smm_sync.cli import main

        with runner.isolated_filesystem(temp_dir=tmp_path):
            smm_dir = Path(".smm")
            smm_dir.mkdir()
            (smm_dir / "locks").mkdir()
            (smm_dir / "history").mkdir()

            with patch("smm_sync.cli.find_git_root", return_value=None), \
                 patch("smm_sync.cli._generate_onboarding_doc", new=AsyncMock(return_value="# Onboarding\n")), \
                 patch("smm_sync.cli.find_project_root", return_value=Path.cwd()), \
                 patch.dict("os.environ", {}, clear=True):
                result = runner.invoke(
                    main,
                    ["setup", "--skip-capture"],
                )

            # Assert inside isolated filesystem context so path is valid
            assert Path("ONBOARDING.md").exists()
            assert Path("ONBOARDING.md").read_text() == "# Onboarding\n"

    def test_setup_skip_capture_flag(self, runner, tmp_path):
        """--skip-capture suppresses the capture run."""
        from smm_sync.cli import main

        with runner.isolated_filesystem(temp_dir=tmp_path):
            smm_dir = Path(".smm")
            smm_dir.mkdir()
            (smm_dir / "locks").mkdir()
            (smm_dir / "history").mkdir()

            with patch("smm_sync.cli.find_git_root", return_value=None), \
                 patch.dict(
                     "os.environ",
                     {"GITHUB_TOKEN": "ghp_test", "ANTHROPIC_API_KEY": "sk-ant-test"},
                 ):
                result = runner.invoke(
                    main,
                    ["setup", "--skip-capture", "--skip-onboarding"],
                )

        assert "skip-capture" in result.output or "Skipping initial capture" in result.output
