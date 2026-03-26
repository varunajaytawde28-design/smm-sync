"""Semantic drift detection between CLAUDE.md versions.

STUB — Month 3. Not yet implemented.
"""
from __future__ import annotations


def detect_drift(old_claude_md: str, new_claude_md: str) -> list[dict]:
    """Detect semantic drift between two versions of CLAUDE.md.

    STUB: Will be implemented in Month 3 using sentence-transformer embeddings.
    Identifies when architectural decisions or constraints have changed in
    meaning (not just text) to catch silent context corruption between sessions.

    Args:
        old_claude_md: Previous CLAUDE.md content as string.
        new_claude_md: New CLAUDE.md content as string.

    Returns:
        List of drift event dicts with keys:
            section (str): Section name that drifted.
            old_text (str): Previous section text.
            new_text (str): New section text.
            severity (str): "low" | "medium" | "high".

    Raises:
        NotImplementedError: Always — not yet implemented.
    """
    # Month 3 plan:
    # 1. Split both CLAUDE.md versions into sections by ## headers
    # 2. Embed each section using sentence-transformers (all-MiniLM-L6-v2)
    # 3. Compute cosine similarity between matching sections
    # 4. Flag sections with similarity < 0.85 as drifted
    # 5. Classify severity: <0.85 low, <0.70 medium, <0.50 high
    raise NotImplementedError(
        "drift.detect_drift() is not yet implemented. "
        "Planned for Month 3: embedding-based semantic drift detection."
    )
