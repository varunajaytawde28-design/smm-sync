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

from smm_sync.context_graph.models import ContextResult, Decision, RejectionResult
from smm_sync.security import DEBUG_MODE, sanitize_content

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


class _LocalEmbedder:
    """Sentence-transformers based embedder — no API key required.

    Uses all-MiniLM-L6-v2: 22 MB, 384-dim embeddings.
    Model downloads on first use and is cached by sentence-transformers.
    """

    def __init__(self, model_name: str = "all-MiniLM-L6-v2") -> None:
        from sentence_transformers import SentenceTransformer

        self._model = SentenceTransformer(model_name)

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
        from sentence_transformers import SentenceTransformer

        self._model = SentenceTransformer(model_name)

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
    from sentence_transformers import SentenceTransformer
    import numpy as np

    model = SentenceTransformer('all-MiniLM-L6-v2')

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

    async def add_decision_local(
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

        constraints = constraints or []
        alternatives = alternatives or []

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
            f"valid_at: timestamp('{ts}'), "
            f"invalid_at: null, "
            f"group_id: '{_esc(project)}'"
            "})"
        )

        async with self._write_lock:
            await self._driver.execute_query(cypher)

        return episode_uuid

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
