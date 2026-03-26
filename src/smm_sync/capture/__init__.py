"""GitHub passive capture for CaaS.

Exports:
    GitHubCapture: Main capture class.
    run_capture: Convenience function to run capture once.
"""
from __future__ import annotations

from smm_sync.capture.github_capture import GitHubCapture, load_config, load_capture_state

__all__ = ["GitHubCapture", "load_config", "load_capture_state"]
