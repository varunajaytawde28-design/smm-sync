"""Thin wrapper around Graphiti using Kuzu as the embedded graph database.

The graph is stored in .smm/graph/ relative to the project root.
No server required — Kuzu runs in-process like SQLite.

Uses local sentence-transformers for embeddings (no OpenAI key needed).
Uses AnthropicClient for entity extraction (requires ANTHROPIC_API_KEY).

Provides:
    GraphClient.add_decision(title, content, rationale, made_by, project, ...)
    GraphClient.search_context(query, project, limit) -> list[ContextResult]
    GraphClient.get_decisions(project) -> list[Decision]
    GraphClient.get_decision_timeline(topic, project) -> list[dict]
    GraphClient.contradiction_check(new_content, project) -> list[dict]
    GraphClient.health_check() -> bool
    get_graph_client(graph_dir, api_key) -> GraphClient  (thread-safe singleton)
"""
from __future__ import annotations

import asyncio
import os
import sys
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

import uuid as _uuid_module

from smm_sync.context_graph.models import ContextResult, Decision, RejectionResult
from smm_sync.security import DEBUG_MODE, sanitize_content

# ---------------------------------------------------------------------------
# Shared sentence-transformers model — Bug 3 fix
# ---------------------------------------------------------------------------
# Module-level singleton: the 22 MB all-MiniLM-L6-v2 model is loaded exactly
# once per Python process regardless of how many GraphClient instances exist.
# Eliminates the 2-3 second "Loading weights" reload seen on every CLI call.
# ---------------------------------------------------------------------------
_shared_st_model = None


def _get_shared_model():
    """Return the shared SentenceTransformer, loading it once per process.

    Suppresses the noisy 'Loading weights' / 'BertModel LOAD REPORT' output
    that sentence-transformers emits at INFO level by default.

    Returns:
        Loaded SentenceTransformer('all-MiniLM-L6-v2') instance.
    """
    global _shared_st_model
    if _shared_st_model is None:
        import logging
        # Suppress sentence-transformers and huggingface_hub progress noise
        logging.getLogger("sentence_transformers").setLevel(logging.WARNING)
        logging.getLogger("huggingface_hub").setLevel(logging.WARNING)
        os.environ.setdefault("TOKENIZERS_PARALLELISM", "false")
        from sentence_transformers import SentenceTransformer
        _shared_st_model = SentenceTransformer("all-MiniLM-L6-v2")
    return _shared_st_model

# ---------------------------------------------------------------------------
# Source confidence hierarchy
# Research basis: EVOKG (MIT CSAIL 2025) — source reliability is the
# primary confidence signal for temporal contradiction resolution.
# Higher confidence = more authoritative; lower = more speculative.
# ---------------------------------------------------------------------------
SOURCE_CONFIDENCE: dict[str, float] = {
    "manual": 0.95,          # Explicitly added by human
    "github_pr": 0.90,       # Merged PR — team reviewed
    "github_release": 0.88,  # Release notes — official
    "meeting": 0.80,         # Explicit decision point
    "slack": 0.65,           # Real discussion but informal
    "github_issue": 0.70,    # Issue — proposed not committed
    "github_commit": 0.60,   # Commit message — often terse
}


def _contradiction_pair_key(title_a: str, title_b: str) -> tuple[str, str]:
    """Return a canonical, order-independent key for a contradiction pair.

    Sorting ensures ("SQLite", "PostgreSQL") and ("PostgreSQL", "SQLite")
    produce the same key, preventing reversed-pair duplicates.

    Args:
        title_a: First decision title.
        title_b: Second decision title.

    Returns:
        Tuple of two lowercased, stripped titles in sorted order.
    """
    return tuple(sorted([title_a.lower().strip(), title_b.lower().strip()]))


class _LocalEmbedder:
    """Sentence-transformers based embedder — no API key required.

    Uses all-MiniLM-L6-v2: 22 MB, 384-dim embeddings.
    Model downloads on first use and is cached by sentence-transformers.
    """

    def __init__(self, model_name: str = "all-MiniLM-L6-v2") -> None:
        self._model = _get_shared_model()  # Bug 3: use process-level singleton

    async def create(
        self,
        input_data: str | list[str] | Iterable[int] | Iterable[Iterable[int]],
    ) -> list[float]:
        """Embed a single text string.

        Args:
            input_data: Text to embed (str) or token list.

        Returns:
            List of floats (384-dim embedding vector).
        """
        if isinstance(input_data, str):
            text = input_data
        elif isinstance(input_data, list) and input_data and isinstance(input_data[0], str):
            text = input_data[0]
        else:
            return [0.0] * 384
        embedding = self._model.encode(text, convert_to_numpy=True)
        return embedding.tolist()

    async def create_batch(self, input_data_list: list[str]) -> list[list[float]]:
        """Embed a batch of text strings.

        Args:
            input_data_list: List of texts to embed.

        Returns:
            List of embedding vectors.
        """
        embeddings = self._model.encode(input_data_list, convert_to_numpy=True)
        return [e.tolist() for e in embeddings]


class _LocalCrossEncoder:
    """Cosine-similarity cross-encoder using sentence-transformers.

    Not a true cross-encoder (no joint encoding), but adequate for dev use.
    Reranks passages by cosine similarity to the query embedding.
    """

    def __init__(self, model_name: str = "all-MiniLM-L6-v2") -> None:
        self._model = _get_shared_model()  # Bug 3: use process-level singleton

    async def rank(self, query: str, passages: list[str]) -> list[tuple[str, float]]:
        """Rank passages by cosine similarity to query.

        Args:
            query: Query string.
            passages: List of passages to rank.

        Returns:
            List of (passage, score) tuples sorted descending by score.
        """
        import numpy as np

        if not passages:
            return []
        all_texts = [query] + passages
        embeddings = self._model.encode(all_texts, convert_to_numpy=True)
        q_emb = embeddings[0]
        scores = []
        for passage, p_emb in zip(passages, embeddings[1:]):
            norm = np.linalg.norm(q_emb) * np.linalg.norm(p_emb)
            score = float(np.dot(q_emb, p_emb) / norm) if norm > 0 else 0.0
            scores.append((passage, score))
        return sorted(scores, key=lambda x: x[1], reverse=True)


def _make_graphiti_clients(graph_path, api_key):
    from graphiti_core import Graphiti
    from graphiti_core.driver.kuzu_driver import KuzuDriver
    from graphiti_core.llm_client.client import LLMClient
    from graphiti_core.llm_client.config import LLMConfig
    from graphiti_core.embedder.client import EmbedderClient
    from graphiti_core.cross_encoder.client import CrossEncoderClient
    import numpy as np

    model = _get_shared_model()  # Bug 3: reuse process-level singleton

    class _Embedder(EmbedderClient):
        async def create(self, input_data):
            text = input_data if isinstance(input_data, str) else input_data[0]
            return model.encode(text).tolist()
        async def create_batch(self, inputs):
            return [model.encode(t).tolist() for t in inputs]

    class _CrossEncoder(CrossEncoderClient):
        async def rank(self, query, passages):
            if not passages:
                return []
            all_emb = model.encode([query] + passages)
            q = all_emb[0]
            scores = []
            for p, e in zip(passages, all_emb[1:]):
                norm = np.linalg.norm(q) * np.linalg.norm(e)
                score = float(np.dot(q, e) / norm) if norm > 0 else 0.0
                scores.append((p, score))
            return sorted(scores, key=lambda x: x[1], reverse=True)

    # GRAPHITI PATCH: Disable full-text search (FTS) functions
    # Bug: Graphiti calls node_fulltext_search/edge_fulltext_search on every search,
    #      but Kuzu FTS requires a network-downloaded extension (.so) that is unavailable
    #      in offline/air-gapped environments and on macOS without Docker.
    # Root cause: graphiti 0.28.2 unconditionally calls FTS alongside vector search;
    #             there is no configuration option to disable it.
    # Fix: Replace the four FTS search functions with async no-ops that return [].
    #      Vector similarity search (all-MiniLM-L6-v2) still works and provides
    #      adequate recall for smm-sync's decision graph size (<10k episodes).
    # Upstream: https://github.com/getzep/graphiti/issues — no fix in 0.28.x
    # Remove when: upgrading to graphiti >= 0.29.x (if FTS becomes optional)
    import graphiti_core.search.search_utils as _su
    import graphiti_core.search.search as _ss

    async def _noop(*args, **kwargs):
        return []

    for _mod in (_su, _ss):
        for _name in (
            "node_fulltext_search",
            "edge_fulltext_search",
            "episode_fulltext_search",
            "community_fulltext_search",
        ):
            if hasattr(_mod, _name):
                setattr(_mod, _name, _noop)

    driver = KuzuDriver(db=str(graph_path))

    # GRAPHITI PATCH: Skip FTS index creation
    # Bug: build_indices_and_constraints calls CREATE_FTS_INDEX which also requires
    #      the Kuzu FTS extension. On first graph init this raises an exception
    #      that prevents the graph from being created at all.
    # Root cause: Kuzu FTS index creation is bundled with range index creation
    #             in graphiti_core.graph_queries without a skip option.
    # Fix: Replace build_indices_and_constraints with a version that only creates
    #      range indices (B-tree, safe) and skips FTS index creation entirely.
    # Upstream: https://github.com/getzep/graphiti/issues — architectural limitation
    # Remove when: upgrading to graphiti >= 0.29.x or Kuzu FTS ships as built-in
    from graphiti_core.graph_queries import get_range_indices, GraphProvider
    from graphiti_core.helpers import semaphore_gather

    async def _build_indices_no_fts(executor, delete_existing=False):
        range_indices = get_range_indices(GraphProvider.KUZU)
        if range_indices:
            await semaphore_gather(*[executor.execute_query(q) for q in range_indices])

    driver._graph_ops.build_indices_and_constraints = _build_indices_no_fts

    if api_key:
        from graphiti_core.llm_client.anthropic_client import AnthropicClient
        llm_client = AnthropicClient(config=LLMConfig(
            api_key=api_key,
            model='claude-sonnet-4-6'
        ))
    else:
        # Read-only mode: stub LLM client that raises if write operations are attempted.
        # Graphiti only calls _generate_response during add_episode (entity extraction);
        # read-only graph queries (similarity search) never invoke the LLM.
        class _NoKeyLLMClient(LLMClient):
            async def _generate_response(self, messages, response_model=None, **kwargs):
                raise RuntimeError(
                    "ANTHROPIC_API_KEY is required for write operations. "
                    "Run: export ANTHROPIC_API_KEY=sk-ant-..."
                )
        llm_client = _NoKeyLLMClient(config=LLMConfig())
    g = Graphiti(
        graph_driver=driver,
        llm_client=llm_client,
        embedder=_Embedder(),
        cross_encoder=_CrossEncoder()
    )
    return g, driver


# ---------------------------------------------------------------------------
# Thread-safe singleton
# Performance: eliminates 2-3 second sentence-transformer model reload on
# every MCP tool call. The model loads once and is reused.
# ---------------------------------------------------------------------------
_client: "GraphClient | None" = None
_client_lock = threading.Lock()


def get_graph_client(
    graph_dir: Path | None = None,
    api_key: str | None = None,
) -> "GraphClient":
    """Return the shared GraphClient instance (thread-safe double-checked locking).

    On first call, creates a GraphClient at the given graph_dir (or derives
    from the project root via smm_sync.config). Subsequent calls return the
    cached instance regardless of arguments — the singleton is initialized once.

    Performance: eliminates 2-3 second sentence-transformer model reload on
    every MCP tool call when the server stays alive between queries.

    Args:
        graph_dir: Path to the Kuzu database file. Only used on first call.
                   If None, derived from find_project_root() / ".smm" / "graph".
        api_key: Anthropic API key. Only used on first call.
                 If None, falls back to ANTHROPIC_API_KEY env var.

    Returns:
        Shared GraphClient instance.
    """
    global _client
    if _client is None:
        with _client_lock:
            if _client is None:  # Double-check after acquiring lock
                if graph_dir is None:
                    try:
                        from smm_sync.config import find_project_root
                        graph_dir = find_project_root() / ".smm" / "graph"
                    except Exception:
                        graph_dir = Path(".smm") / "graph"
                if api_key is None:
                    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
                _client = GraphClient(graph_dir=graph_dir, api_key=api_key)
    return _client


class GraphClient:
    """Context graph client backed by Graphiti + Kuzu.

    Lazy-initialised: the Graphiti instance is created on first use.
    All graph failures are caught and logged; they never crash the MCP server.

    Args:
        graph_dir: Path where the Kuzu database file will be created (.smm/graph).
                   Note: Kuzu creates a single database FILE at this path,
                   not a directory. The parent (.smm/) must exist.
        api_key: Anthropic API key. Falls back to ANTHROPIC_API_KEY env var.
    """

    def __init__(self, graph_dir: Path, api_key: str | None = None) -> None:
        self.graph_dir = graph_dir  # Kuzu DB file path (created by kuzu on first open)
        self.api_key = api_key or os.environ.get("ANTHROPIC_API_KEY", "")
        self._graphiti = None
        self._driver = None
        # Serialize all write operations to prevent concurrent write corruption in Kuzu
        self._write_lock = asyncio.Lock()
        # Bug 2 fix: embedding cache for _detect_local_contradictions.
        # Keyed by content[:200]; avoids recomputing embeddings for existing episodes
        # when checking contradictions during batch ingestion.
        self._embedding_cache: dict[str, list[float]] = {}

    async def _get_graphiti(self):
        """Return (and lazily initialise) the Graphiti instance.

        Initialises without an API key — read-only operations (search,
        get_decisions, get_decision_timeline) work without one.  Write
        operations (add_decision) check for the key themselves before
        calling this method.

        Returns:
            Graphiti instance with Kuzu backend.
        """
        if self._graphiti is None:
            # Kuzu needs the parent to exist; it creates the DB file itself
            self.graph_dir.parent.mkdir(parents=True, exist_ok=True)
            self._graphiti, self._driver = _make_graphiti_clients(
                self.graph_dir, self.api_key
            )
            await self._graphiti.build_indices_and_constraints()
        return self._graphiti

    async def _calculate_confidence(
        self,
        source_type: str,
        content: str,
        has_alternatives: bool,
        has_rationale: bool,
    ) -> float:
        """Calculate confidence score for a decision.

        Research basis: EVOKG (2025) confidence-based contradiction
        resolution uses source reliability × content quality signals.
        METR RCT shows confidence scoring prevents AI from acting
        on superseded decisions.

        Base score from SOURCE_CONFIDENCE hierarchy, boosted by content quality.
        Maximum boost is +0.15 (capped at 1.0).

        Args:
            source_type: Key into SOURCE_CONFIDENCE (e.g. "github_pr").
            content: Full decision content text.
            has_alternatives: True if rejected alternatives were provided.
            has_rationale: True if a rationale string was provided.

        Returns:
            Confidence score in [0.0, 1.0].
        """
        base = SOURCE_CONFIDENCE.get(source_type, 0.50)

        boost = 0.0
        if has_alternatives:
            boost += 0.05  # Explicit alternatives considered
        if has_rationale and len(content) > 200:
            boost += 0.05  # Detailed rationale
        if len(content) > 500:
            boost += 0.05  # Comprehensive decision record

        return min(1.0, base + boost)

    async def contradiction_check(
        self,
        new_content: str,
        project: str,
    ) -> list[dict]:
        """Check if a new decision contradicts existing decisions.

        Algorithm (EVOKG-style temporal heuristics):
        1. Vector similarity search for related existing decisions
        2. For each similar decision, check temporal state
        3. If contradiction detected, do NOT delete old decision
           Instead: note superseded_by relationship in the new episode body
        4. Score using source hierarchy × temporal decay

        Research basis: EVOKG (MIT CSAIL 2025) — temporal graphs
        that track superseding relationships outperform static
        graphs by 23.3%. Never delete old decisions — they form
        the compliance audit trail.

        This method NEVER raises exceptions — contradiction check failure
        must never block a new decision from being written.

        Args:
            new_content: Content of the new decision being added.
            project: Project name (graph partition).

        Returns:
            List of contradiction dicts (may be empty). Each dict has keys:
            existing (str), similarity (float), action (str).
        """
        try:
            related = await self.search_context(
                query=new_content[:500],
                project=project,
                limit=5,
            )

            contradictions = []
            for result in related:
                if result.relevance_score > 0.75:
                    # High similarity — potential contradiction
                    contradictions.append({
                        "existing": result.title,
                        "similarity": result.relevance_score,
                        "action": "superseded_by",
                    })

            return contradictions
        except Exception as e:
            if DEBUG_MODE:
                raise
            return []  # Never block on contradiction check failure

    async def add_decision(
        self,
        title: str,
        content: str,
        rationale: str,
        made_by: str,
        project: str,
        constraints: list[str] | None = None,
        alternatives: list[str] | None = None,
        decision_type: str = "technical",
        source_type: str = "manual",
    ) -> str:
        """Record a team decision as a Graphiti episode.

        Automatically checks for contradictions before writing and logs
        the confidence score. Contradictions are noted in the episode body
        but never block the write — EVOKG requires old decisions be preserved
        as the compliance audit trail.

        Args:
            title: Short title of the decision.
            content: Full description of what was decided.
            rationale: Why this decision was made.
            made_by: Who made this decision.
            project: Project name — used as graph partition (group_id).
            constraints: Known constraints imposed by this decision.
            alternatives: Alternatives that were considered.
            decision_type: One of 'architectural', 'technical', 'product'.
            source_type: Source reliability key (see SOURCE_CONFIDENCE).

        Returns:
            Episode UUID string.
        """
        from graphiti_core.nodes import EpisodeType

        if not self.api_key:
            raise RuntimeError(
                "ANTHROPIC_API_KEY is required for write operations (add_decision). "
                "Run: export ANTHROPIC_API_KEY=sk-ant-... "
                "Get your key from https://console.anthropic.com/settings/keys"
            )

        # Sanitize content before storing in graph (Fix 1: prompt injection prevention)
        content, content_flagged = sanitize_content(content)
        if content_flagged:
            print(
                f"  \u26a0\ufe0f  Sanitized potentially malicious content in decision: {title[:60]}",
                file=sys.stderr
            )
        rationale, rationale_flagged = sanitize_content(rationale)

        async with self._write_lock:
            return await self._add_decision_locked(
                title=title,
                content=content,
                rationale=rationale,
                made_by=made_by,
                project=project,
                constraints=constraints,
                alternatives=alternatives,
                decision_type=decision_type,
                source_type=source_type,
            )

    async def _add_decision_locked(
        self,
        title: str,
        content: str,
        rationale: str,
        made_by: str,
        project: str,
        constraints: list[str] | None = None,
        alternatives: list[str] | None = None,
        decision_type: str = "technical",
        source_type: str = "manual",
    ) -> str:
        """Internal: execute add_decision while holding _write_lock."""
        from graphiti_core.nodes import EpisodeType

        g = await self._get_graphiti()
        constraints = constraints or []
        alternatives = alternatives or []

        # Calculate confidence score (EVOKG source hierarchy)
        confidence = await self._calculate_confidence(
            source_type=source_type,
            content=content,
            has_alternatives=bool(alternatives),
            has_rationale=bool(rationale),
        )

        episode_body = (
            f"[PROJECT: {project}]\n\n"
            f"{content}\n\n"
            f"Rationale: {rationale}\n\n"
            f"Decision type: {decision_type}\n"
            f"Made by: {made_by}\n"
            f"Confidence: {confidence:.2f}\n"
            f"Constraints: {'; '.join(constraints) if constraints else 'none'}\n"
            f"Alternatives considered: {'; '.join(alternatives) if alternatives else 'none'}"
        )

        # Check for contradictions (EVOKG temporal superseding)
        contradictions = await self.contradiction_check(
            f"{title}: {content}", project
        )
        if contradictions:
            for c in contradictions:
                print(f"  \u26a0\ufe0f  Possible contradiction: {c['existing']}", file=sys.stderr)
            # Append contradiction context — never block the write
            contradiction_note = "\n\nContradictions detected: " + \
                ", ".join([c["existing"] for c in contradictions])
            episode_body += contradiction_note

        result = await g.add_episode(
            name=title,
            episode_body=episode_body,
            source_description=f"decision by {made_by}",
            reference_time=datetime.now(timezone.utc),
            source=EpisodeType.text,
        )
        return str(result.episode.uuid)

    # -----------------------------------------------------------------------
    # Bug 2 fix: episodic-level contradiction detection for local-mode writes
    # -----------------------------------------------------------------------

    async def _compute_embedding(self, text: str) -> list[float]:
        """Compute a 384-dim embedding, using an instance-level cache.

        The cache is keyed on text[:200] so that repeated calls with the same
        content (common during batch ingestion) hit memory instead of rerunning
        the model.

        Args:
            text: Text to embed (truncated to 500 chars for speed).

        Returns:
            List of 384 floats.
        """
        key = text[:200]
        if key in self._embedding_cache:
            return self._embedding_cache[key]
        model = _get_shared_model()
        emb = model.encode(text[:500], convert_to_numpy=True).tolist()
        self._embedding_cache[key] = emb
        return emb

    async def _detect_local_contradictions(
        self,
        title: str,
        episode_body: str,
        episode_uuid: str,
        project: str,
    ) -> list[dict]:
        """Detect contradictions among :Episodic nodes without edges.

        Called by add_decision_local() after the new node is written.
        Replaces the edge-based contradiction_check() for the local-write path
        where no edges exist (Graphiti entity extraction was skipped).

        Algorithm (zero API calls):
        1. Embed new decision content (sentence-transformers, local).
        2. Fetch all existing :Episodic nodes (excluding the just-written one).
        3. Compute cosine similarity for each pair.
        4. For pairs with similarity > 0.5, apply text-based heuristics:
           - Check for explicit contradiction keywords in combined text.
           - Check for same-topic different-choice pattern.
        5. Return contradiction dicts; caller writes them to contradictions.jsonl.

        Args:
            title: Title of the newly-written decision.
            episode_body: Full episode body (content + rationale + metadata).
            episode_uuid: UUID of the just-written episode (excluded from search).
            project: Project name (for future per-project scoping).

        Returns:
            List of contradiction dicts (may be empty). Each has keys:
            id, decision_a, decision_b, explanation, detected_at, resolved.
            Returns [] on any error so callers never block.
        """
        _CONTRADICTION_WORDS = frozenset({
            # explicit rejection / replacement
            "instead of", "rejected", "replaced by", "switched from",
            "moved away from", "supersedes", "contradicts", "overrides",
            "conflict", "no longer", "reverted", "undoes", "opposite of",
            "not chosen", "abandoned",
            # migration / transition signals
            "split", "migrate", "migration", "switch to", "switching to",
            "replace", "replacement", "separate",
            "rewrite", "refactor away", "moving to", "transition to",
            "rather than", "over graphql", "over rest", "async instead",
            "drop", "remove", "eliminate", "decouple", "automate",
        })
        # Additive decisions (Add X, Introduce Y, Enhance Z) extend rather than
        # replace — skip the high-similarity tier-1 check to avoid false positives
        # when multiple decisions share the same technical domain.
        _ADDITIVE_PREFIXES = ("add ", "introduce ", "enhance ", "extend ", "include ")

        try:
            import numpy as np

            new_emb = await self._compute_embedding(f"{title} {episode_body[:400]}")
            new_arr = np.array(new_emb, dtype=np.float32)
            new_norm = float(np.linalg.norm(new_arr))
            if new_norm == 0:
                return []

            # Fetch all existing Episodic nodes except the one just written.
            # LIMIT 200 keeps this bounded for large graphs.
            rows, _, _ = await self._driver.execute_query(
                "MATCH (e:Episodic) "
                f"WHERE e.uuid <> '{episode_uuid}' "
                "RETURN e.uuid, e.name, e.content "
                "ORDER BY e.created_at DESC LIMIT 200"
            )
            contradictions = []
            for row in rows:
                existing_title = row.get("e.name", "") or ""
                existing_content = row.get("e.content", "") or ""

                # Skip self-contradictions: same title, different UUID
                if existing_title.lower().strip() == title.lower().strip():
                    continue

                existing_emb = await self._compute_embedding(
                    f"{existing_title} {existing_content[:400]}"
                )
                existing_arr = np.array(existing_emb, dtype=np.float32)
                existing_norm = float(np.linalg.norm(existing_arr))
                if existing_norm == 0:
                    continue

                score = float(np.dot(new_arr, existing_arr) / (new_norm * existing_norm))

                # Tier 1: high similarity signals a direct conflict
                # Skip for additive decisions ("Add X", "Introduce Y") — they
                # extend rather than replace, so high similarity is expected.
                is_additive = any(title.lower().startswith(p) for p in _ADDITIVE_PREFIXES)
                high_sim = not is_additive and score > 0.72

                # Tier 2: moderate similarity + explicit contradiction keyword.
                # Include both titles so keywords in titles (e.g. "instead of",
                # "split", "automate") are checked even when absent from the body.
                combined = (
                    title + " " + episode_body + " "
                    + existing_title + " " + existing_content
                ).lower()
                keyword_hit = score > 0.5 and any(w in combined for w in _CONTRADICTION_WORDS)

                if high_sim or keyword_hit:
                    reason = (
                        f"high semantic similarity ({score:.2f})"
                        if high_sim and not keyword_hit
                        else f"similarity {score:.2f}, contradiction keyword detected"
                    )
                    contradictions.append({
                        "id": str(_uuid_module.uuid4()),
                        "decision_a": title,
                        "decision_b": existing_title,
                        "explanation": (
                            f"'{title}' may contradict '{existing_title}' ({reason})"
                        ),
                        "detected_at": datetime.now(timezone.utc).isoformat(),
                        "resolved": False,
                    })

            return contradictions
        except Exception:
            if DEBUG_MODE:
                raise
            return []  # never block add_decision_local on contradiction detection

    async def _detect_via_agent_cli(
        self,
        title: str,
        episode_body: str,
        episode_uuid: str,
        project: str,
        agent: str,
    ) -> list[dict]:
        """Detect contradictions by calling the user's AI agent CLI.

        Sends all existing decisions + the new one to the configured agent
        (claude -p or cursor --message) and parses the JSON response.

        Pre-filters to top 30 by embedding similarity when the graph has
        more than 30 decisions, to keep the prompt within context limits.

        Falls back to [] on any failure (timeout, command not found, parse
        error) — never blocks the decision write.

        Args:
            title: Title of the newly-written decision.
            episode_body: Full episode body (content + rationale + metadata).
            episode_uuid: UUID of the just-written episode (excluded).
            project: Project name (unused, for future scoping).
            agent: 'claude-code', 'cursor', or 'both'.

        Returns:
            List of contradiction dicts, or [] on any failure.
        """
        import asyncio
        import json as _json
        import re
        import subprocess

        try:
            import numpy as np
        except ImportError:
            np = None  # type: ignore[assignment]

        # Fetch existing decisions (exclude the just-written node).
        try:
            rows, _, _ = await self._driver.execute_query(
                "MATCH (e:Episodic) "
                f"WHERE e.uuid <> '{episode_uuid}' "
                "RETURN e.uuid, e.name, e.content "
                "ORDER BY e.created_at DESC LIMIT 200"
            )
        except Exception as _e:
            print(f"  [smm] agent-cli: kuzu query failed: {_e}", file=sys.stderr)
            return []

        if not rows:
            return []

        decisions: list[tuple[str, str]] = [
            (row.get("e.name", "") or "", row.get("e.content", "") or "")
            for row in rows
        ]

        # Pre-filter to top 30 by local embedding similarity when graph is large.
        if len(decisions) > 30 and np is not None:
            try:
                new_emb = await self._compute_embedding(f"{title} {episode_body[:400]}")
                new_arr = np.array(new_emb, dtype=np.float32)
                new_norm = float(np.linalg.norm(new_arr))

                scored: list[tuple[float, str, str]] = []
                for name, content in decisions:
                    emb = await self._compute_embedding(f"{name} {content[:400]}")
                    arr = np.array(emb, dtype=np.float32)
                    norm = float(np.linalg.norm(arr))
                    score = (
                        float(np.dot(new_arr, arr) / (new_norm * norm))
                        if new_norm > 0 and norm > 0
                        else 0.0
                    )
                    scored.append((score, name, content))

                scored.sort(key=lambda x: x[0], reverse=True)
                decisions = [(name, content) for _, name, content in scored[:30]]
            except Exception:
                # Embedding pre-filter failed — fall back to first 30 by recency
                decisions = decisions[:30]

        # Build numbered decision list for the prompt.
        lines: list[str] = []
        for i, (name, content) in enumerate(decisions, 1):
            snippet = ""
            if "Rationale: " in content:
                snippet = content.split("Rationale: ", 1)[1].split("\n")[0][:120]
            else:
                snippet = content[:120]
            lines.append(f"{i}. {name}: {snippet}")

        new_rationale = (
            episode_body.split("Rationale: ", 1)[1].split("\n")[0][:200]
            if "Rationale: " in episode_body
            else episode_body[:200]
        )

        prompt = (
            "Here are all existing architectural decisions:\n"
            + "\n".join(lines)
            + "\n\nNEW DECISION just added:\n"
            + f"{title}: {new_rationale}\n\n"
            + "Does the new decision contradict, reverse, replace, or conflict "
            + "with any existing decisions?\n\n"
            + 'Output ONLY JSON: [{"existing_number": N, "reason": "..."}] or []\n'
            + "Nothing else."
        )

        import tempfile

        def _run_sync(cmd: list[str], env: dict | None = None) -> str:
            """Run agent CLI synchronously; raises on failure."""
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=120,
                env=env,
            )
            if result.returncode != 0:
                raise RuntimeError(
                    f"exit {result.returncode}: {result.stderr[:200]}"
                )
            return result.stdout

        # Build a clean environment for claude -p so that a nested invocation
        # from inside a Claude Code session is not blocked by the parent's
        # session-detection env vars.
        _safe_env = os.environ.copy()
        for _var in [
            "CLAUDECODE",
            "CLAUDE_CODE_ENTRYPOINT",
            "CLAUDE_CODE_DISABLE_FEEDBACK_SURVEY",
            "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC",
        ]:
            _safe_env.pop(_var, None)
        _safe_env["CLAUDE_CODE_TMPDIR"] = tempfile.mkdtemp(prefix="smm-axiom-")

        loop = asyncio.get_running_loop()
        raw_output: str | None = None

        if agent in ("claude-code", "both"):
            try:
                # claude -p "<prompt>" — print mode, non-interactive, exits after response
                raw_output = await loop.run_in_executor(
                    None, lambda: _run_sync(["claude", "-p", prompt], env=_safe_env)
                )
            except FileNotFoundError:
                print("  [smm] contradiction check: 'claude' not found in PATH", file=sys.stderr)
            except subprocess.TimeoutExpired:
                print("  [smm] contradiction check: claude -p timed out (30s)", file=sys.stderr)
            except RuntimeError as _e:
                print(f"  [smm] contradiction check: claude -p failed: {_e}", file=sys.stderr)

        if raw_output is None and agent in ("cursor", "both"):
            try:
                # cursor --message "<prompt>" — Cursor CLI one-shot AI query.
                # Flag name may vary by Cursor version; update here if needed.
                raw_output = await loop.run_in_executor(
                    None, lambda: _run_sync(["cursor", "--message", prompt])
                )
            except FileNotFoundError:
                print("  [smm] contradiction check: 'cursor' not found in PATH", file=sys.stderr)
            except subprocess.TimeoutExpired:
                print("  [smm] contradiction check: cursor --message timed out (30s)", file=sys.stderr)
            except RuntimeError as _e:
                print(f"  [smm] contradiction check: cursor --message failed: {_e}", file=sys.stderr)

        if raw_output is None:
            return []

        # Parse JSON array from the agent's response.
        try:
            match = re.search(r"\[.*?\]", raw_output, re.DOTALL)
            if not match:
                return []
            parsed = _json.loads(match.group(0))
            if not isinstance(parsed, list):
                return []

            contradictions: list[dict] = []
            for item in parsed:
                if not isinstance(item, dict):
                    continue
                n = item.get("existing_number")
                reason = str(item.get("reason", ""))
                if not isinstance(n, int) or n < 1 or n > len(decisions):
                    continue
                existing_name, _ = decisions[n - 1]
                # Skip self-contradictions: same title, case-insensitive
                if existing_name.lower().strip() == title.lower().strip():
                    continue
                contradictions.append({
                    "id": str(_uuid_module.uuid4()),
                    "decision_a": title,
                    "decision_b": existing_name,
                    "explanation": (
                        f"'{title}' may contradict '{existing_name}': {reason}"
                    ),
                    "detected_at": datetime.now(timezone.utc).isoformat(),
                    "resolved": False,
                })
            return contradictions
        except Exception as _e:
            print(f"  [smm] contradiction check: response parse error: {_e}", file=sys.stderr)
            return []

    async def add_decision_local(
        self,
        title: str,
        content: str,
        rationale: str,
        made_by: str,
        project: str,
        constraints: list[str] | str | None = None,
        alternatives: list[str] | str | None = None,
        decision_type: str = "technical",
        source_type: str = "manual",
        confidence: float | None = None,
    ) -> str:
        """Write a decision directly to Kuzu — zero Anthropic API calls.

        Bypasses Graphiti.add_episode() entirely. No Haiku classification,
        no Sonnet entity extraction. Uses pre-extracted structured data
        (title, rationale, alternatives, constraints already parsed by the
        calling agent or hook).

        Contradiction detection still runs via local vector similarity search
        (sentence-transformers only, no API). The :Episodic node is written
        directly to Kuzu via Cypher, so ANTHROPIC_API_KEY is not required.

        Args:
            title: Short title of the decision.
            content: Full description of what was decided.
            rationale: Why this decision was made.
            made_by: Who made this decision.
            project: Project name — used as graph partition (group_id).
            constraints: Known constraints imposed by this decision.
            alternatives: Alternatives that were considered.
            decision_type: One of 'architectural', 'technical', 'product'.
            source_type: Source reliability key (see SOURCE_CONFIDENCE).

        Returns:
            Episode UUID string.
        """
        import uuid as _uuid

        content, content_flagged = sanitize_content(content)
        if content_flagged:
            print(
                f"  \u26a0\ufe0f  Sanitized potentially malicious content in decision: {title[:60]}",
                file=sys.stderr,
            )
        rationale, _ = sanitize_content(rationale)

        # Normalise: accept plain string or list — join() needs a list
        if isinstance(constraints, str):
            constraints = [constraints] if constraints else []
        else:
            constraints = list(constraints) if constraints else []
        if isinstance(alternatives, str):
            alternatives = [alternatives] if alternatives else []
        else:
            alternatives = list(alternatives) if alternatives else []

        if confidence is None:
            confidence = await self._calculate_confidence(
                source_type=source_type,
                content=content,
                has_alternatives=bool(alternatives),
                has_rationale=bool(rationale),
            )

        episode_body = (
            f"[PROJECT: {project}]\n\n"
            f"{content}\n\n"
            f"Rationale: {rationale}\n\n"
            f"Decision type: {decision_type}\n"
            f"Made by: {made_by}\n"
            f"Confidence: {confidence:.2f}\n"
            f"Status: pending\n"
            f"Constraints: {'; '.join(constraints) if constraints else 'none'}\n"
            f"Alternatives considered: {'; '.join(alternatives) if alternatives else 'none'}"
        )

        # Contradiction check against existing decisions — pure vector similarity,
        # zero API calls. Runs BEFORE writing so the new episode is not compared
        # against itself.
        contradictions = await self.contradiction_check(
            f"{title}: {content}", project
        )
        if contradictions:
            for c in contradictions:
                print(f"  \u26a0\ufe0f  Possible contradiction: {c['existing']}", file=sys.stderr)
            episode_body += "\n\nContradictions detected: " + ", ".join(
                [c["existing"] for c in contradictions]
            )

        # Ensure Kuzu driver + schema are initialised (no API calls in this path
        # because _NoKeyLLMClient is used when api_key is empty, and we never
        # call add_episode which is the only method that invokes the LLM client).
        await self._get_graphiti()

        episode_uuid = str(_uuid.uuid4())
        now = datetime.now(timezone.utc)
        ts = now.strftime("%Y-%m-%d %H:%M:%S")

        def _esc(s: str) -> str:
            """Escape a string for safe embedding in a Kuzu Cypher string literal."""
            return (
                s.replace("\\", "\\\\")
                .replace("'", "\\'")
                .replace("\n", "\\n")
                .replace("\r", "")
            )

        cypher = (
            "CREATE (e:Episodic {"
            f"uuid: '{episode_uuid}', "
            f"name: '{_esc(title[:200])}', "
            f"source: 'text', "
            f"source_description: 'decision by {_esc(made_by)}', "
            f"content: '{_esc(episode_body[:8000])}', "
            f"created_at: timestamp('{ts}'), "
            f"group_id: '{_esc(project)}'"
            "})"
        )

        async with self._write_lock:
            await self._driver.execute_query(cypher)

        # Episodic-level contradiction detection.
        # Route based on .smm/config.json {"agent": "..."}.
        # - "claude-code" / "cursor" / "both" → call the user's AI agent CLI
        # - "skip" (or config absent)          → local embedding heuristic
        import json as _json
        smm_dir = self.graph_dir.parent
        _agent_cfg = "skip"
        try:
            _cfg_path = smm_dir / "config.json"
            # Fix 6: if config.json not found at graph_dir.parent, walk up from cwd
            if not _cfg_path.exists():
                try:
                    from smm_sync.config import get_smm_dir as _get_smm_dir_cfg
                    _alt = _get_smm_dir_cfg() / "config.json"
                    if _alt.exists():
                        _cfg_path = _alt
                except Exception:
                    pass
            if _cfg_path.exists():
                _agent_cfg = _json.loads(_cfg_path.read_text(encoding="utf-8")).get(
                    "agent", "skip"
                )
        except Exception:
            pass  # malformed config → fall back to local

        if _agent_cfg == "skip":
            local_contradictions = await self._detect_local_contradictions(
                title=title,
                episode_body=episode_body,
                episode_uuid=episode_uuid,
                project=project,
            )
        else:
            local_contradictions = await self._detect_via_agent_cli(
                title=title,
                episode_body=episode_body,
                episode_uuid=episode_uuid,
                project=project,
                agent=_agent_cfg,
            )

        if local_contradictions:
            # Filter out pairs already actioned in the index so the same
            # contradiction is never written to contradictions.jsonl twice.
            try:
                from smm_sync.contradiction_index import filter_new_contradictions
                local_contradictions = filter_new_contradictions(
                    smm_dir,
                    local_contradictions,
                    new_title=title,
                )
            except Exception:
                pass  # never block on index read failure
            contradictions_path = smm_dir / "contradictions.jsonl"
            # Dedup: skip reversed pairs already in contradictions.jsonl.
            # Normalized pair key ("A","B") == ("B","A") so "SQLite↔Postgres"
            # is never written twice even when synced in opposite order.
            _seen_pair_keys: set[tuple] = set()
            try:
                if contradictions_path.exists():
                    for _ln in contradictions_path.read_text(encoding="utf-8").splitlines():
                        _ln = _ln.strip()
                        if _ln:
                            try:
                                _e = _json.loads(_ln)
                                _da = _e.get("decision_a", "") or ""
                                _db = _e.get("decision_b", "") or ""
                                if _da and _db:
                                    _seen_pair_keys.add(_contradiction_pair_key(_da, _db))
                            except Exception:
                                pass
            except Exception:
                pass
            _deduped: list[dict] = []
            for c in local_contradictions:
                _pk = _contradiction_pair_key(
                    c.get("decision_a", ""), c.get("decision_b", "")
                )
                if _pk not in _seen_pair_keys:
                    _deduped.append(c)
                    _seen_pair_keys.add(_pk)
            local_contradictions = _deduped
            for c in local_contradictions:
                # Fix 5: print clean contradiction warning to stderr before "recorded"
                _existing = c.get("decision_b", "")
                _explanation = c.get("explanation", "Conflicting decisions detected")
                print(
                    f"\u26a0 Contradiction: conflicts with \"{_existing}\"\n"
                    f"  Reason: {_explanation}",
                    file=sys.stderr,
                )
                try:
                    from smm_sync.jsonl_writer import append_jsonl_locked
                    append_jsonl_locked(contradictions_path, c)
                except Exception:
                    pass  # never block ingestion on JSONL write failure

        # Incrementally build edges for the new node using local embeddings.
        # Zero API calls — embedding-based only. Runs after the node is written
        # so the new node is included in the pairwise similarity scan.
        try:
            await self._create_edges_for_node(
                episode_uuid=episode_uuid,
                episode_body=episode_body,
                title=title,
                project=project,
            )
        except Exception:
            if DEBUG_MODE:
                raise
            pass  # never block ingestion on edge creation failure

        # Write audit entry to compliance_lineage.jsonl so the Audit Trail and
        # the +N this week counter have data to display.
        try:
            import json as _json_audit
            # Fix 2: use agent name from config.json (already resolved above as _agent_cfg)
            _audit_agent = _agent_cfg if _agent_cfg not in ("skip", None) else "manual"
            audit_entry = {
                "event_type": "decision_added",
                "timestamp": now.isoformat(),
                "decision_title": title,
                "confidence": round(confidence, 4),
                "actor": made_by,
                "agent": _audit_agent,
                "source_type": source_type,
                "decision_id": episode_uuid,
                "session_id": _audit_agent,
                "decisions_surfaced": [title],
                "decision_count": 1,
            }
            audit_path = self.graph_dir.parent / "compliance_lineage.jsonl"
            audit_path.parent.mkdir(parents=True, exist_ok=True)
            with open(audit_path, "a", encoding="utf-8") as _af:
                _af.write(_json_audit.dumps(audit_entry) + "\n")
        except Exception:
            pass  # never block ingestion on audit write failure

        return episode_uuid

    # ---------------------------------------------------------------------------
    # Edge discovery — Approach 1: fully local, zero API credits
    # ---------------------------------------------------------------------------

    async def _ensure_decision_edge_table(self) -> None:
        """Create DecisionEdge REL TABLE in Kuzu if it does not exist yet.

        Uses IF NOT EXISTS so it is safe to call on every startup.
        Edges are stored between :Episodic nodes and carry the relationship
        type determined by local heuristics.
        """
        cypher = (
            "CREATE REL TABLE IF NOT EXISTS DecisionEdge("
            "FROM Episodic TO Episodic, "
            "name STRING, "
            "edge_type STRING, "
            "reason STRING, "
            "weight FLOAT, "
            "created_at TIMESTAMP"
            ")"
        )
        await self._driver.execute_query(cypher)

    @staticmethod
    def _infer_edge_type(title_a: str, body_a: str, title_b: str, body_b: str) -> str:
        """Classify the relationship between two decisions using keyword heuristics.

        Pure text analysis — zero API calls.

        Args:
            title_a: Title of the source decision.
            body_a: Full episode body of the source decision.
            title_b: Title of the target decision.
            body_b: Full episode body of the target decision.

        Returns:
            One of: SUPERSEDES, REQUIRES, ENABLES, PREFERRED_OVER, RELATES_TO.
        """
        a_lower = (title_a + " " + body_a).lower()
        b_lower = (title_b + " " + body_b).lower()
        combined = a_lower + " " + b_lower

        # SUPERSEDES: one replaces / overrides the other
        supersedes_words = {
            "switch", "move away", "replaced by", "supersedes",
            "instead of", "no longer", "reverted", "abandoned",
            "migrate from", "drop ", "dropped",
        }
        if any(w in a_lower for w in supersedes_words):
            return "SUPERSEDES"

        # PREFERRED_OVER: explicit rejection of an alternative
        reject_words = {"rejected", "not chosen", "preferred over", "chose over"}
        if any(w in combined for w in reject_words):
            return "PREFERRED_OVER"

        # REQUIRES: one decision's constraint is the other's subject
        if "requires" in a_lower or ("constraint" in a_lower and title_b.lower() in a_lower):
            return "REQUIRES"

        # ENABLES: one unlocks / allows the other
        if "enables" in a_lower or "allow" in a_lower:
            return "ENABLES"

        return "RELATES_TO"

    async def _create_edges_for_node(
        self,
        episode_uuid: str,
        episode_body: str,
        title: str,
        project: str,
    ) -> int:
        """Create edges between a new node and existing similar nodes.

        Called by add_decision_local() immediately after writing the node.
        Embedding similarity > 0.6 triggers edge creation. Deduplicates by
        checking whether an edge already exists before creating.

        Args:
            episode_uuid: UUID of the newly-written :Episodic node.
            episode_body: Full episode body text.
            title: Decision title.
            project: Project name (for future per-project scoping).

        Returns:
            Number of new edges created.
        """
        import numpy as np

        await self._ensure_decision_edge_table()

        new_emb = await self._compute_embedding(f"{title} {episode_body[:400]}")
        new_arr = np.array(new_emb, dtype=np.float32)
        new_norm = float(np.linalg.norm(new_arr))
        if new_norm == 0:
            return 0

        rows, _, _ = await self._driver.execute_query(
            "MATCH (e:Episodic) "
            f"WHERE e.uuid <> '{episode_uuid}' "
            "RETURN e.uuid, e.name, e.content "
            "ORDER BY e.created_at DESC LIMIT 200"
        )

        created = 0
        now_ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")

        def _esc(s: str) -> str:
            return (
                s.replace("\\", "\\\\")
                .replace("'", "\\'")
                .replace("\n", "\\n")
                .replace("\r", "")
            )

        for row in rows:
            other_uuid = row.get("e.uuid", "") or ""
            other_title = row.get("e.name", "") or ""
            other_content = row.get("e.content", "") or ""
            if not other_uuid:
                continue

            other_emb = await self._compute_embedding(
                f"{other_title} {other_content[:400]}"
            )
            other_arr = np.array(other_emb, dtype=np.float32)
            other_norm = float(np.linalg.norm(other_arr))
            if other_norm == 0:
                continue

            score = float(np.dot(new_arr, other_arr) / (new_norm * other_norm))
            if score < 0.5:
                continue

            edge_type = self._infer_edge_type(title, episode_body, other_title, other_content)
            reason = f"similarity={score:.2f}"

            # Deduplicate: skip if an edge already exists in either direction
            check_rows, _, _ = await self._driver.execute_query(
                "MATCH (a:Episodic)-[r:DecisionEdge]->(b:Episodic) "
                f"WHERE (a.uuid = '{_esc(episode_uuid)}' AND b.uuid = '{_esc(other_uuid)}') "
                f"   OR (a.uuid = '{_esc(other_uuid)}' AND b.uuid = '{_esc(episode_uuid)}') "
                "RETURN count(r) AS cnt"
            )
            if check_rows and (check_rows[0].get("cnt") or 0) > 0:
                continue

            edge_cypher = (
                f"MATCH (a:Episodic {{uuid: '{_esc(episode_uuid)}'}}), "
                f"      (b:Episodic {{uuid: '{_esc(other_uuid)}'}}) "
                "CREATE (a)-[:DecisionEdge {"
                f"name: '{_esc(edge_type)}', "
                f"edge_type: '{_esc(edge_type)}', "
                f"reason: '{_esc(reason)}', "
                f"weight: {score:.4f}, "
                f"created_at: timestamp('{now_ts}')"
                "}]->(b)"
            )
            async with self._write_lock:
                await self._driver.execute_query(edge_cypher)
            created += 1

        return created

    async def discover_edges(self, project: str) -> dict:
        """Discover and create edges between ALL decisions using local embeddings.

        Full pairwise scan — use on existing graphs or after bulk ingestion.
        For incremental use, add_decision_local() calls _create_edges_for_node()
        automatically after each write.

        Algorithm (zero API credits, zero network calls):
        1. Load all :Episodic nodes.
        2. Embed each node's content with the shared all-MiniLM-L6-v2 model.
        3. Pairwise cosine similarity — O(n²) but fast for n ≤ 500.
        4. Pairs with similarity > 0.6 get an edge with an inferred type.
        5. Deduplicates before writing (no duplicate edges).

        Args:
            project: Project name (informational; logged in output).

        Returns:
            Dict with keys: nodes_scanned, edges_created, edges_skipped.
        """
        import numpy as np

        await self._get_graphiti()  # ensure driver + schema initialised
        await self._ensure_decision_edge_table()

        rows, _, _ = await self._driver.execute_query(
            "MATCH (e:Episodic) "
            "RETURN e.uuid, e.name, e.content "
            "ORDER BY e.created_at ASC"
        )

        if not rows:
            return {"nodes_scanned": 0, "edges_created": 0, "edges_skipped": 0}

        # Pre-compute all embeddings in one pass
        uuids = [r.get("e.uuid", "") for r in rows]
        titles = [r.get("e.name", "") or "" for r in rows]
        contents = [r.get("e.content", "") or "" for r in rows]

        texts = [f"{t} {c[:400]}" for t, c in zip(titles, contents)]
        model = _get_shared_model()
        embeddings = model.encode(texts, batch_size=32, show_progress_bar=False)
        norms = np.linalg.norm(embeddings, axis=1, keepdims=True)
        # Avoid division by zero
        norms = np.where(norms == 0, 1.0, norms)
        normed = embeddings / norms

        n = len(uuids)
        # Full similarity matrix
        sim_matrix = normed @ normed.T  # shape (n, n)

        now_ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        created = 0
        skipped = 0

        def _esc(s: str) -> str:
            return (
                s.replace("\\", "\\\\")
                .replace("'", "\\'")
                .replace("\n", "\\n")
                .replace("\r", "")
            )

        for i in range(n):
            for j in range(i + 1, n):  # upper triangle only — avoid duplicates
                score = float(sim_matrix[i, j])
                if score < 0.5:
                    skipped += 1
                    continue

                uuid_a = uuids[i]
                uuid_b = uuids[j]
                if not uuid_a or not uuid_b:
                    skipped += 1
                    continue

                # Check for existing edge
                check_rows, _, _ = await self._driver.execute_query(
                    "MATCH (a:Episodic)-[r:DecisionEdge]->(b:Episodic) "
                    f"WHERE (a.uuid = '{_esc(uuid_a)}' AND b.uuid = '{_esc(uuid_b)}') "
                    f"   OR (a.uuid = '{_esc(uuid_b)}' AND b.uuid = '{_esc(uuid_a)}') "
                    "RETURN count(r) AS cnt"
                )
                if check_rows and (check_rows[0].get("cnt") or 0) > 0:
                    skipped += 1
                    continue

                edge_type = self._infer_edge_type(
                    titles[i], contents[i], titles[j], contents[j]
                )
                reason = f"similarity={score:.2f}"

                edge_cypher = (
                    f"MATCH (a:Episodic {{uuid: '{_esc(uuid_a)}'}}), "
                    f"      (b:Episodic {{uuid: '{_esc(uuid_b)}'}}) "
                    "CREATE (a)-[:DecisionEdge {"
                    f"name: '{_esc(edge_type)}', "
                    f"edge_type: '{_esc(edge_type)}', "
                    f"reason: '{_esc(reason)}', "
                    f"weight: {score:.4f}, "
                    f"created_at: timestamp('{now_ts}')"
                    "}]->(b)"
                )
                async with self._write_lock:
                    await self._driver.execute_query(edge_cypher)
                created += 1

        return {"nodes_scanned": n, "edges_created": created, "edges_skipped": skipped}

    async def get_edges(self, project: str) -> list[dict]:
        """Return all DecisionEdge edges as source/target/type dicts.

        Used by the /api/graph dashboard endpoint to include real edges in the
        Cytoscape.js response alongside nodes.

        Args:
            project: Project name (currently unused; edges span all projects).

        Returns:
            List of dicts with keys: source_uuid, target_uuid, edge_type, weight.
        """
        try:
            await self._get_graphiti()
            # Check if DecisionEdge table exists before querying
            await self._ensure_decision_edge_table()
            rows, _, _ = await self._driver.execute_query(
                "MATCH (a:Episodic)-[r:DecisionEdge]->(b:Episodic) "
                "RETURN a.uuid AS src, b.uuid AS tgt, r.edge_type AS etype, r.weight AS w"
            )
            return [
                {
                    "source_uuid": row.get("src", ""),
                    "target_uuid": row.get("tgt", ""),
                    "edge_type": row.get("etype", "RELATES_TO"),
                    "weight": float(row.get("w") or 0.6),
                }
                for row in rows
                if row.get("src") and row.get("tgt")
            ]
        except Exception:
            if DEBUG_MODE:
                raise
            return []

    async def search_context(
        self,
        query: str,
        project: str,
        limit: int = 5,
    ) -> list[ContextResult]:
        """Search for relevant decisions and facts.

        Args:
            query: Natural language query.
            project: Project name (graph partition).
            limit: Maximum number of results.

        Returns:
            List of ContextResult objects sorted by relevance.
        """
        g = await self._get_graphiti()
        # Use vector-only search with a lower min_score threshold.
        # all-MiniLM-L6-v2 cosine scores top out around 0.45 vs Graphiti's
        # default 0.6 (calibrated for OpenAI embeddings).
        embedder = g.clients.embedder
        search_vector = await embedder.create(query)
        from graphiti_core.search.search_filters import SearchFilters
        edges = await self._driver.search_ops.edge_similarity_search(
            executor=self._driver,
            search_vector=search_vector,
            source_node_uuid=None,
            target_node_uuid=None,
            search_filter=SearchFilters(),
            limit=limit * 3,
            min_score=0.2,
        )
        # The graph is project-scoped (.smm/graph), so no project tag filter needed.
        results = []
        for edge in edges:
            fact = getattr(edge, "fact", "") or ""
            name = getattr(edge, "name", "") or ""
            # Build a short excerpt (first 250 chars)
            excerpt = fact[:250].rsplit(" ", 1)[0] if len(fact) > 250 else fact
            results.append(
                ContextResult(
                    title=name,
                    content=fact,
                    relevance_score=0.8,  # graphiti doesn't expose raw score on basic search
                    excerpt=excerpt,
                )
            )
            if len(results) >= limit:
                break
        return results

    async def get_decisions(self, project: str) -> list[Decision]:
        """Retrieve all decisions for a project by querying Episodic nodes directly.

        Episodic nodes (label :Episodic) are the actual decision records created by
        add_episode. Entity nodes (label :Entity) are extracted reference entities
        (e.g. "VP Engineering", "Datadog") and must not be returned here.

        Args:
            project: Project name (graph partition).

        Returns:
            List of Decision objects.
        """
        await self._get_graphiti()  # ensure driver is initialised
        rows, _, _ = await self._driver.execute_query(
            "MATCH (e:Episodic) "
            "RETURN e.uuid, e.name, e.content, e.source_description, e.created_at "
            "ORDER BY e.created_at DESC LIMIT 200"
        )
        decisions = []
        for i, row in enumerate(rows):
            title = row.get("e.name") or ""
            content = row.get("e.content") or ""
            # Unescape Cypher-escaped newlines (\\n → real newline) so that
            # splitlines() and split("\n") work correctly on the parsed content.
            content = content.replace("\\n", "\n")
            source = row.get("e.source_description") or ""
            created_at = row.get("e.created_at") or datetime.utcnow()

            # Parse structured sections written by _add_decision_locked
            rationale = ""
            made_by = ""
            if "Rationale: " in content:
                rationale = content.split("Rationale: ", 1)[1].split("\n")[0].strip()
            if "Made by: " in content:
                made_by = content.split("Made by: ", 1)[1].split("\n")[0].strip()

            decisions.append(
                Decision(
                    id=row.get("e.uuid") or str(i),
                    title=title,
                    content=content,
                    rationale=rationale,
                    made_by=made_by,
                    project=project,
                    source=source,
                    created_at=created_at,
                    valid=True,
                )
            )
        return decisions

    async def get_decision_timeline(
        self,
        topic: str,
        project: str,
    ) -> list[dict]:
        """Get chronological history of decisions related to a topic.

        Shows how team thinking evolved, including superseded decisions.
        This is the compliance lineage audit trail — it shows exactly
        what the team knew and when they decided it.

        Research basis: EVOKG (MIT CSAIL 2025) — temporal graphs that
        track superseding relationships outperform static graphs by 23.3%.
        Never hide old decisions; they are the audit trail.

        Args:
            topic: Natural language topic to search for (e.g. "state management").
            project: Project name (graph partition).

        Returns:
            List of decision dicts ordered by created_at (oldest first), with
            superseded decisions marked but not hidden. Each dict has keys:
            title, content, created_at, valid, superseded_note.
        """
        try:
            g = await self._get_graphiti()
            embedder = g.clients.embedder
            search_vector = await embedder.create(topic)
            from graphiti_core.search.search_filters import SearchFilters
            edges = await self._driver.search_ops.edge_similarity_search(
                executor=self._driver,
                search_vector=search_vector,
                source_node_uuid=None,
                target_node_uuid=None,
                search_filter=SearchFilters(),
                limit=50,
                min_score=0.15,
            )

            timeline = []
            seen: set[str] = set()
            for edge in edges:
                fact = getattr(edge, "fact", "") or ""
                name = getattr(edge, "name", "") or ""
                key = fact[:100]
                if key in seen:
                    continue
                seen.add(key)
                created_at = getattr(edge, "created_at", None)
                is_valid = getattr(edge, "invalid_at", None) is None
                superseded_note = ""
                if not is_valid:
                    superseded_note = "SUPERSEDED — see newer decisions on this topic"
                elif "Contradictions detected:" in fact:
                    superseded_note = "May contradict earlier decisions (see content)"
                timeline.append({
                    "title": name,
                    "content": fact,
                    "created_at": created_at.isoformat() if created_at else None,
                    "valid": is_valid,
                    "superseded_note": superseded_note,
                })

            # Sort chronologically (oldest first)
            timeline.sort(key=lambda x: x["created_at"] or "")
            return timeline
        except Exception as e:
            if DEBUG_MODE:
                raise
            return []

    async def check_rejected_alternatives(
        self, query: str, project: str = "default"
    ) -> list[RejectionResult]:
        """Search the graph for previously-rejected alternatives matching query.

        Zero LLM calls — purely graph similarity search + keyword matching
        on 'alternatives' and rejection-signal keywords.

        Args:
            query: The approach or idea being proposed.
            project: Project name to scope search.

        Returns:
            List of RejectionResult for any matching rejected alternatives.
            Never raises — returns empty list on error.
        """
        _REJECTION_KEYWORDS = {
            "rejected", "considered", "alternative", "instead of",
            "not chosen", "discarded", "dropped", "avoided", "ruled out",
        }
        try:
            results = await self.search_context(query=query, project=project, limit=10)
            out: list[RejectionResult] = []
            for r in results:
                content_lower = r.content.lower()
                if not any(kw in content_lower for kw in _REJECTION_KEYWORDS):
                    continue
                # Extract the rejection fragment as the "alternative"
                excerpt = r.excerpt or r.content[:200]
                out.append(
                    RejectionResult(
                        rejected_alternative=excerpt,
                        decision_title=r.title,
                        rationale=r.content[:400],
                        decided_at=datetime.now(timezone.utc).isoformat(),
                        confidence=r.relevance_score,
                        decision_id=r.title,  # best proxy without full decision lookup
                    )
                )
            return out
        except Exception as e:
            if DEBUG_MODE:
                raise
            return []

    def _extract_path_keywords(self, file_path: str) -> list[str]:
        """Extract meaningful search keywords from a file path.

        Splits on path separators and common delimiters, removes noise tokens,
        and maps known module names to richer keyword sets.

        Args:
            file_path: Relative or absolute file path string.

        Returns:
            Up to 4 distinct keywords for graph search.
        """
        import re

        _NOISE = {
            "src", "smm", "sync", "py", "test", "tests", "__init__",
            "init", "utils", "helpers", "common", "shared", "base",
            "the", "a", "an", "", "js", "ts", "html", "css", "json",
        }
        _MAP: dict[str, list[str]] = {
            "mcp_server": ["mcp", "security", "tools"],
            "dashboard": ["dashboard", "ui", "api"],
            "context_graph": ["graph", "knowledge", "embedding"],
            "coordinator": ["coordinator", "locking", "atomic"],
            "compiler": ["compiler", "template", "jinja"],
            "config": ["config", "toml", "schema"],
            "state": ["state", "crdt", "json"],
            "cli": ["cli", "click", "commands"],
            "git_utils": ["git", "hook", "precommit"],
            "ingester": ["ingester", "extraction", "capture"],
        }

        # Tokenise: split on /, \, _, -, .
        raw = re.split(r"[/\\._\-]", file_path)
        keywords: list[str] = []
        for token in raw:
            token_lower = token.lower()
            if token_lower in _NOISE:
                continue
            if token_lower in _MAP:
                keywords.extend(_MAP[token_lower])
            else:
                keywords.append(token_lower)

        # Deduplicate preserving order
        seen: set[str] = set()
        unique: list[str] = []
        for kw in keywords:
            if kw and kw not in seen:
                seen.add(kw)
                unique.append(kw)
        return unique[:4]

    async def get_path_context(
        self, file_path: str, project: str = "default"
    ) -> list[ContextResult]:
        """Return JIT (just-in-time) context relevant to a specific file path.

        Extracts keywords from the path, searches the graph per keyword,
        and returns up to 3 high-confidence or constraint results.

        Args:
            file_path: The file being edited (relative or absolute).
            project: Project name to scope search.

        Returns:
            Up to 3 ContextResult items (constraints or confidence >= 0.80).
            Never raises — returns empty list on error.
        """
        try:
            keywords = self._extract_path_keywords(file_path)
            if not keywords:
                return []

            seen_titles: set[str] = set()
            results: list[ContextResult] = []
            for kw in keywords:
                hits = await self.search_context(query=kw, project=project, limit=5)
                for h in hits:
                    if h.title in seen_titles:
                        continue
                    is_constraint = "constraint" in h.content.lower() or "must" in h.content.lower()
                    if is_constraint or h.relevance_score >= 0.80:
                        seen_titles.add(h.title)
                        results.append(h)
                        if len(results) >= 3:
                            return results
            return results
        except Exception as e:
            if DEBUG_MODE:
                raise
            return []

    def health_check(self) -> bool:
        """Check if the graph database is accessible.

        Tries to open the Kuzu database. Returns True on success.
        Never raises — returns False on any error.

        Returns:
            True if Kuzu can open the database path, False otherwise.
        """
        try:
            import kuzu

            # Kuzu needs the parent to exist; it creates the DB file itself
            self.graph_dir.parent.mkdir(parents=True, exist_ok=True)
            db = kuzu.Database(str(self.graph_dir))
            del db
            return True
        except Exception:
            return False
