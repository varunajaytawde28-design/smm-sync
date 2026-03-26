"""Tests for smm_sync.coordinator."""
import pytest
from pathlib import Path

from smm_sync import coordinator


@pytest.fixture
def smm_dir(tmp_path):
    """Temporary .smm directory."""
    d = tmp_path / ".smm"
    d.mkdir()
    return d


def test_claim_returns_true_on_success(smm_dir):
    assert coordinator.claim(smm_dir, "auth.py", "session-1") is True


def test_claim_returns_false_if_already_claimed(smm_dir):
    coordinator.claim(smm_dir, "auth.py", "session-1")
    assert coordinator.claim(smm_dir, "auth.py", "session-2") is False


def test_is_claimed_true_after_claim(smm_dir):
    coordinator.claim(smm_dir, "auth.py", "session-1")
    assert coordinator.is_claimed(smm_dir, "auth.py") is True


def test_is_claimed_false_before_claim(smm_dir):
    assert coordinator.is_claimed(smm_dir, "auth.py") is False


def test_release_removes_claim(smm_dir):
    coordinator.claim(smm_dir, "auth.py", "session-1")
    coordinator.release(smm_dir, "auth.py")
    assert coordinator.is_claimed(smm_dir, "auth.py") is False


def test_release_nonexistent_does_not_raise(smm_dir):
    coordinator.release(smm_dir, "nonexistent.py")


def test_reclaim_after_release_succeeds(smm_dir):
    coordinator.claim(smm_dir, "auth.py", "session-1")
    coordinator.release(smm_dir, "auth.py")
    assert coordinator.claim(smm_dir, "auth.py", "session-2") is True


def test_list_claimed_empty(smm_dir):
    assert coordinator.list_claimed(smm_dir) == []


def test_list_claimed_contains_filepath(smm_dir):
    coordinator.claim(smm_dir, "auth.py", "session-1")
    claimed = coordinator.list_claimed(smm_dir)
    assert len(claimed) == 1
    assert claimed[0]["filepath"] == "auth.py"


def test_list_claimed_contains_session(smm_dir):
    coordinator.claim(smm_dir, "auth.py", "session-abc")
    claimed = coordinator.list_claimed(smm_dir)
    assert claimed[0]["session"] == "session-abc"


def test_list_claimed_multiple_files(smm_dir):
    coordinator.claim(smm_dir, "auth.py", "session-1")
    coordinator.claim(smm_dir, "db.py", "session-2")
    claimed = coordinator.list_claimed(smm_dir)
    assert len(claimed) == 2
    filepaths = {c["filepath"] for c in claimed}
    assert "auth.py" in filepaths
    assert "db.py" in filepaths


def test_claim_different_files_independently(smm_dir):
    """Claiming one file does not affect another."""
    assert coordinator.claim(smm_dir, "auth.py", "s1") is True
    assert coordinator.claim(smm_dir, "db.py", "s2") is True


def test_claim_path_with_slash(smm_dir):
    """Filepaths with slashes are handled safely."""
    assert coordinator.claim(smm_dir, "src/auth.py", "s1") is True
    assert coordinator.is_claimed(smm_dir, "src/auth.py") is True
