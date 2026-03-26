"""Compliance lineage logging for CaaS.

Exports:
    LineageLogger: Append-only audit trail logger.
    get_lineage_logger: Module-level singleton accessor.
"""
from __future__ import annotations

from pathlib import Path

from smm_sync.compliance.lineage import LineageLogger

__all__ = ["LineageLogger", "get_lineage_logger"]

_logger: LineageLogger | None = None


def get_lineage_logger(log_path: Path | None = None) -> LineageLogger:
    """Return the shared LineageLogger instance.

    Thread-safe lazy singleton. On first call, creates the logger at
    .smm/compliance_lineage.jsonl relative to the project root.

    Args:
        log_path: Override the log file path (used in tests). If None,
                  derives the path from the project root via smm_sync.config.

    Returns:
        Shared LineageLogger instance.
    """
    global _logger
    if _logger is None:
        if log_path is None:
            try:
                from smm_sync.config import get_smm_dir

                log_path = get_smm_dir() / "compliance_lineage.jsonl"
            except Exception:
                log_path = Path(".smm") / "compliance_lineage.jsonl"
        _logger = LineageLogger(log_path)
    return _logger
