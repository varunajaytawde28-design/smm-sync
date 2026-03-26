"""File watcher for automatic smm.toml change detection.

STUB — Month 1. Not yet implemented.
"""
from __future__ import annotations

from pathlib import Path
from typing import Callable


def watch(smm_dir: Path, on_change: Callable[[], None]) -> None:
    """Watch for changes to smm.toml and invoke callback on modification.

    STUB: Will be implemented in Month 1 using watchdog.
    Will detect smm.toml modifications and trigger `smm compile` automatically.

    Args:
        smm_dir: Path to .smm directory (parent contains smm.toml).
        on_change: Callback invoked with no arguments when smm.toml changes.

    Raises:
        NotImplementedError: Always — not yet implemented.
    """
    # Month 1 plan:
    # 1. Use watchdog.observers.Observer to watch smm_dir.parent
    # 2. On FileModifiedEvent for smm.toml: call on_change()
    # 3. Debounce with 500ms delay to avoid double-fires
    # 4. Run in daemon thread so it doesn't block the CLI
    raise NotImplementedError(
        "watcher.watch() is not yet implemented. "
        "Planned for Month 1: watchdog-based smm.toml observer."
    )
