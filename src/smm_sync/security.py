"""Content sanitization for prompt injection prevention.

Scans content before storing in the knowledge graph to prevent
indirect prompt injection via malicious GitHub PRs, commits, or issues.

Usage:
    from smm_sync.security import sanitize_content

    clean, flagged = sanitize_content(raw_content)
    if flagged:
        print("Warning: content was sanitized", file=sys.stderr)
"""
from __future__ import annotations

import hashlib
import re
import sys

import os

DEBUG_MODE = os.environ.get('CAAS_DEBUG', '').lower() in ('1', 'true', 'yes')

INJECTION_PATTERNS = [
    r"ignore\s+(all\s+)?(previous|prior|above)",
    r"you\s+(must|should|are\s+required\s+to)\s+",
    r"disregard\s+(all\s+)?",
    r"forget\s+(everything|all)",
    r"new\s+instruction",
    r"system\s*:\s*",
    r"<\s*system\s*>",
    r"\[INST\]",
    r"execute\s+.{0,50}(command|shell|bash|sh\b)",
    r"output\s+.{0,50}(contents?\s+of|the\s+file)",
    r"~/\.",
    r"id_rsa",
    r"\.ssh",
    r"passwd",
    r"shadow",
    r"base64\s+decode",
]


def sanitize_content(content: str) -> tuple[str, bool]:
    """Scan content for prompt injection patterns before storing in the knowledge graph.

    Args:
        content: Raw content string to sanitize.

    Returns:
        (sanitized_content, was_flagged) where:
        - sanitized_content has suspicious lines replaced with [CONTENT FILTERED]
        - was_flagged is True if any patterns were detected

    If flagged:
    - Logs warning to stderr with content hash (never logs the actual content)
    - Replaces suspicious line with [CONTENT FILTERED]
    - Still processes remaining clean content
    - Never raises — silent sanitization only
    """
    if not content:
        return "", False

    flagged = False
    lines = content.split('\n')
    clean_lines = []

    for line in lines:
        line_flagged = False
        for pattern in INJECTION_PATTERNS:
            if re.search(pattern, line, re.IGNORECASE):
                content_hash = hashlib.sha256(
                    line.encode()
                ).hexdigest()[:8]
                print(
                    f"  \u26a0\ufe0f  Injection pattern detected "
                    f"(hash:{content_hash}) \u2014 filtered",
                    file=sys.stderr
                )
                clean_lines.append("[CONTENT FILTERED]")
                flagged = True
                line_flagged = True
                break
        if not line_flagged:
            clean_lines.append(line)

    return '\n'.join(clean_lines), flagged
