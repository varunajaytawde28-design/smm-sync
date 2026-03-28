"""Tuple Space coordinator using os.rename() POSIX atomicity for file claiming.
"""
from __future__ import annotations

import os
import time
from pathlib import Path


def _locks_dir(smm_dir: Path) -> Path:
    """Return the .smm/locks/ directory, creating it if needed.

    Args:
        smm_dir: Path to .smm directory.

    Returns:
        Path to .smm/locks/ directory.
    """
    d = smm_dir / "locks"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _lock_file_path(smm_dir: Path, filepath: str) -> Path:
    """Return the lock file path for a given filepath.

    Args:
        smm_dir: Path to .smm directory.
        filepath: Relative path of the file to lock.

    Returns:
        Path to the .lock file representing this claim.
    """
    safe = filepath.replace("/", "__").replace("\\", "__").replace(":", "__")
    return _locks_dir(smm_dir) / f"{safe}.lock"


def claim(smm_dir: Path, filepath: str, session_id: str = "") -> bool:
    """Atomically claim a file using os.rename() POSIX atomicity.

    Uses a write-to-tmp then rename pattern. os.rename() on POSIX
    is atomic within the same filesystem — either the rename succeeds
    and we own the lock, or the lock already exists and we fail.

    Args:
        smm_dir: Path to .smm directory.
        filepath: Relative path of file to claim.
        session_id: Optional identifier for the claiming session.

    Returns:
        True if claim succeeded, False if file is already claimed.
    """
    lock_path = _lock_file_path(smm_dir, filepath)
    tmp_path = lock_path.with_suffix(".tmp")

    content = f"filepath={filepath}\nsession={session_id}\ntimestamp={time.time()}\n"
    tmp_path.write_text(content, encoding="utf-8")

    try:
        os.link(tmp_path, lock_path)  # fails with EEXIST if lock_path exists
        os.unlink(tmp_path)
        return True
    except OSError:
        tmp_path.unlink(missing_ok=True)
        return False


def release(smm_dir: Path, filepath: str) -> None:
    """Release a claimed file.

    Args:
        smm_dir: Path to .smm directory.
        filepath: Relative path of file to release.
    """
    _lock_file_path(smm_dir, filepath).unlink(missing_ok=True)


def list_claimed(smm_dir: Path) -> list[dict]:
    """Return all currently claimed files with their metadata.

    Args:
        smm_dir: Path to .smm directory.

    Returns:
        List of dicts with keys: filepath, session, timestamp.
    """
    locks_dir = _locks_dir(smm_dir)
    results = []
    for lock_file in sorted(locks_dir.glob("*.lock")):
        try:
            content = lock_file.read_text(encoding="utf-8")
            entry: dict = {}
            for line in content.splitlines():
                if "=" in line:
                    k, v = line.split("=", 1)
                    entry[k.strip()] = v.strip()
            results.append(entry)
        except OSError:
            pass
    return results


def is_claimed(smm_dir: Path, filepath: str) -> bool:
    """Check if a file is currently claimed.

    Args:
        smm_dir: Path to .smm directory.
        filepath: Relative path of file to check.

    Returns:
        True if file is claimed by any session, False otherwise.
    """
    return _lock_file_path(smm_dir, filepath).exists()
