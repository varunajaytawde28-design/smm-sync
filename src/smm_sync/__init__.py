"""SMM-Sync — shared context for simultaneous AI agents."""
__version__ = "0.1.0"


def _bin_stub() -> None:
    """Python stub entry-point for smm-fast-write script.

    When the maturin-compiled Rust binary is on PATH it will shadow this stub.
    This fallback locates the binary at several candidate paths and exec()s it,
    or falls back to the pure-Python jsonl_writer if none is found.
    """
    import os
    import shutil
    import sys
    from pathlib import Path

    # Candidate locations for the compiled Rust binary.
    candidates = []
    # 1. Explicit override via env var.
    if os.environ.get("SMM_FAST_WRITE_BIN"):
        candidates.append(Path(os.environ["SMM_FAST_WRITE_BIN"]))
    # 2. Binary installed on PATH (maturin develop / pip install).
    which = shutil.which("smm-fast-write")
    if which:
        candidates.append(Path(which))
    # 3. Local dev build inside the repo.
    candidates.append(
        Path(__file__).parents[3] / "rust_cli" / "target" / "release" / "smm-fast-write"
    )

    for candidate in candidates:
        if candidate.is_file() and os.access(str(candidate), os.X_OK):
            os.execv(str(candidate), [str(candidate)] + sys.argv[1:])

    # Fallback: pure-Python JSONL writer reading from stdin.
    import json
    from smm_sync.jsonl_writer import write_decision

    try:
        data = json.load(sys.stdin)
    except json.JSONDecodeError as exc:
        print(f"smm-fast-write: invalid JSON: {exc}", file=sys.stderr)
        sys.exit(1)

    try:
        write_decision(data)
        print(f"\u2713 Decision: {data.get('title', '?')} \u2014 recorded")
    except Exception as exc:
        print(f"smm-fast-write: {exc}", file=sys.stderr)
        sys.exit(1)
