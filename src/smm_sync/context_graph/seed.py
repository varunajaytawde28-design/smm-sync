"""Seed the context graph with 18 interconnected Axiom Hub decisions.

Each episode body explicitly names other decisions using relationship
keywords (SUPERSEDES, REQUIRES, ENABLES, CONTRADICTS, PREFERRED_OVER,
SUPPORTS, CONSTRAINS, FIXES) so Graphiti's entity extraction creates
Entity nodes AND edges between them — producing a connected graph.

WARNING: Calls add_episode which makes LLM API calls (Anthropic).
Seeding 18 decisions makes approximately 54-126 API calls.
This is expected and will take 5-10 minutes to complete.

Usage:
    asyncio.run(seed_test_data(graph_client, project="smm-sync"))
"""
from __future__ import annotations

import asyncio
import time

from smm_sync.context_graph.client import GraphClient

# ---------------------------------------------------------------------------
# Cross-referencing decisions for Axiom Hub / smm-sync.
#
# DESIGN: Every episode body names related decisions explicitly.
# Graphiti's LLM entity extraction (Haiku + Sonnet) reads the body
# and creates Entity nodes for each named decision and Edge nodes for
# the relationship between them.  Without these cross-references the
# graph contains only isolated Episodic nodes with no Entity edges.
# ---------------------------------------------------------------------------

SEED_DECISIONS = [
    # ── INFRASTRUCTURE ───────────────────────────────────────────────────────
    {
        "title": "Use FalkorDB via Docker for graph database",
        "content": (
            "Initially planned to run FalkorDB via Docker as the embedded graph "
            "database for the development environment. FalkorDB offers 11.4x faster "
            "queries than Neo4j and is the production target for Axiom Hub. "
            "This decision was later SUPERSEDED BY 'Use Kuzu as embedded graph DB for development' "
            "because Docker Desktop cannot be installed on macOS 13 Ventura."
        ),
        "rationale": (
            "FalkorDB is Redis-based and extremely fast for sparse graph traversals. "
            "It was the natural choice for production. However, macOS 13 Ventura blocks "
            "Docker Desktop installation, making local development impossible. "
            "The decision to use FalkorDB via Docker was abandoned in favour of Kuzu."
        ),
        "alternatives": [
            "Neo4j Aura cloud (requires internet)",
            "SQLite with manual graph queries",
        ],
        "constraints": [
            "FalkorDB remains the production target when Docker is available",
            "Migration from Kuzu to FalkorDB must be a one-line config change",
        ],
        "made_by": "Varun, initial infrastructure planning",
        "decision_type": "technical",
    },
    {
        "title": "Use Kuzu as embedded graph DB for development",
        "content": (
            "We SUPERSEDE 'Use FalkorDB via Docker for graph database' with Kuzu "
            "(embedded, file-based) for the development environment. "
            "This decision ENABLES 'Run 100% locally without Docker'. "
            "This decision REQUIRES 'Build on Graphiti rather than custom temporal knowledge graph' "
            "because Graphiti supports Kuzu natively as a backend. "
            "The graph database is stored at .smm/graph/ relative to the project root. "
            "Kuzu runs in-process like SQLite — no server, no ports, works offline."
        ),
        "rationale": (
            "The macOS 13 Ventura constraint prevents Docker Desktop installation. "
            "Kuzu is embedded like SQLite — no server, no ports, runs in-process. "
            "Graphiti supports Kuzu natively. When Docker becomes available, migrating "
            "to FalkorDB is a one-line config change (FalkorDB is 11.4x faster and "
            "better for production)."
        ),
        "alternatives": [
            "FalkorDB via Docker (SUPERSEDED — blocked by macOS 13)",
            "Neo4j Aura free cloud (requires internet)",
            "SQLite with manual graph queries",
        ],
        "constraints": [
            "Graph stored at .smm/graph/ relative to project root",
            "FalkorDB is the production target — Kuzu is dev only",
            "Migration path must be preserved",
        ],
        "made_by": "Varun, forced by macOS 13 constraint",
        "decision_type": "technical",
    },
    {
        "title": "Run 100% locally without Docker",
        "content": (
            "Axiom Hub must run entirely locally with zero Docker dependency during development. "
            "This is ENABLED BY 'Use Kuzu as embedded graph DB for development'. "
            "This decision CONSTRAINS 'All LLM calls: Haiku for classification, Sonnet for extraction' "
            "because the only external dependency permitted is Anthropic API calls. "
            "Local sentence-transformers (all-MiniLM-L6-v2) replace OpenAI embeddings."
        ),
        "rationale": (
            "Developer onboarding friction is the #1 killer of internal tools. "
            "If setup requires Docker, developers on macOS 13 or corporate-locked machines "
            "cannot run the tool at all. Kuzu (embedded) + local sentence-transformers "
            "means: git clone → pip install → works. Zero infrastructure."
        ),
        "alternatives": [
            "Docker Compose for full stack (blocked by macOS 13)",
            "Cloud-hosted graph database (requires internet always)",
        ],
        "constraints": [
            "No Docker dependency in development path",
            "Only external dependency: Anthropic API key (write operations only)",
            "Embeddings use local sentence-transformers, not OpenAI",
        ],
        "made_by": "Varun, developer experience principle",
        "decision_type": "architectural",
    },
    {
        "title": "Build on Graphiti rather than custom temporal knowledge graph",
        "content": (
            "We use Graphiti (by Zep, Apache 2.0) as the graph layer rather than "
            "building our own temporal knowledge graph. "
            "This decision is PREFERRED OVER 'Build custom graph layer on FalkorDB directly' "
            "saving 25-37 weeks of engineering for a solo founder. "
            "This decision REQUIRES 'Use Kuzu as embedded graph DB for development' "
            "as the Kuzu backend for Graphiti. "
            "This decision REQUIRES 'All LLM calls: Haiku for classification, Sonnet for extraction' "
            "because Graphiti uses LLM calls for entity extraction during add_episode."
        ),
        "rationale": (
            "Building a production-ready temporal knowledge graph from scratch would "
            "take 25-37 weeks for a solo founder: entity resolution alone takes weeks "
            "of iteration. Graphiti already solves the hardest problems — bi-temporal "
            "tracking, entity deduplication, conflict detection. Every saved week goes "
            "into the capture and distribution layers where our actual moat lives."
        ),
        "alternatives": [
            "Build custom graph layer on FalkorDB directly (PREFERRED OVER — saves 25-37 weeks)",
            "Use Neo4j directly without Graphiti",
            "PostgreSQL + pgvector only",
        ],
        "constraints": [
            "Graph layer must be swappable via abstraction layer",
            "Must not expose Graphiti API directly to MCP tools",
            "Graphiti ingestion is slow (3-7 LLM calls per episode) — never call in hot path",
        ],
        "made_by": "Varun, based on moat analysis research",
        "decision_type": "architectural",
    },
    # ── LLM COST / PIPELINE ──────────────────────────────────────────────────
    {
        "title": "All LLM calls: Haiku for classification, Sonnet for extraction",
        "content": (
            "Two-tier LLM strategy: claude-haiku-4-5 for fast classification tasks, "
            "claude-sonnet-4-6 for entity extraction and deep analysis. "
            "This decision CONSTRAINS 'Keep API costs under $0.01 per PR' "
            "by using Haiku (10x cheaper) for the high-volume classification stage. "
            "This decision SUPPORTS 'Passive GitHub capture over manual ADRs' "
            "because the two-stage pipeline makes per-PR processing affordable. "
            "This decision is REQUIRED BY 'Build on Graphiti rather than custom temporal knowledge graph' "
            "since Graphiti calls the LLM during entity extraction."
        ),
        "rationale": (
            "Haiku costs 10x less than Sonnet per token. "
            "Classification (is this a decision? is this infrastructure?) is a simple task "
            "Haiku handles perfectly. Entity extraction and relationship mapping require "
            "Sonnet's deeper reasoning. Splitting the pipeline by complexity keeps total "
            "cost under $0.01 per PR while maintaining extraction quality."
        ),
        "alternatives": [
            "Use Sonnet for everything (10x more expensive)",
            "Use Haiku for everything (lower extraction quality)",
            "Use GPT-4o (vendor lock-in to OpenAI)",
        ],
        "constraints": [
            "Classification stage must use Haiku only",
            "Entity extraction may use Sonnet",
            "Total pipeline cost must stay under $0.01 per PR",
        ],
        "made_by": "Varun, cost efficiency analysis",
        "decision_type": "technical",
    },
    {
        "title": "Keep API costs under $0.01 per PR",
        "content": (
            "Total Anthropic API cost per GitHub PR processed must stay under $0.01. "
            "This is CONSTRAINED BY 'All LLM calls: Haiku for classification, Sonnet for extraction'. "
            "This constraint SUPPORTS 'Target buyer: VP Engineering not individual developers' "
            "because enterprise buyers need predictable per-seat costs, not unbounded usage bills. "
            "This constraint REQUIRES 'Confidence hierarchy: manual=0.95, pr=0.90, slack=0.65, commit=0.60' "
            "to avoid re-running expensive extraction on low-confidence sources."
        ),
        "rationale": (
            "At $0.01/PR with 100 PRs/month, the cost is $1/month per team. "
            "Enterprise customers with 1000 PRs/month pay $10/month in API costs. "
            "This is a 100-1000x margin on the $30-50/user/month pricing. "
            "Exceeding this threshold makes the unit economics unworkable."
        ),
        "alternatives": [
            "Accept higher costs and charge per-token (unpredictable for enterprise)",
            "Cache all LLM results aggressively (complex, stale risk)",
        ],
        "constraints": [
            "Hard limit: $0.01 per PR processed",
            "Use Haiku for classification, Sonnet only for extraction",
            "Never reprocess already-classified PRs",
        ],
        "made_by": "Varun, unit economics model",
        "decision_type": "product",
    },
    {
        "title": "Confidence hierarchy: manual=0.95, pr=0.90, slack=0.65, commit=0.60",
        "content": (
            "Source confidence scores: manual entry=0.95, GitHub PR=0.90, "
            "GitHub release=0.88, meeting notes=0.80, Slack=0.65, "
            "GitHub issue=0.70, commit message=0.60. "
            "This hierarchy GOVERNS 'Contradiction detection threshold' — "
            "a new decision only supersedes an old one if its confidence is higher. "
            "This is REQUIRED BY 'Keep API costs under $0.01 per PR' "
            "to avoid reprocessing high-confidence decisions with low-confidence sources. "
            "This hierarchy SUPPORTS 'Passive GitHub capture over manual ADRs' "
            "by giving PR-sourced decisions high confidence (0.90) without manual input."
        ),
        "rationale": (
            "Research basis: EVOKG (MIT CSAIL 2025) — source reliability is the "
            "primary confidence signal for temporal contradiction resolution. "
            "Manual entries are highest confidence because humans reviewed them. "
            "PRs are second because the team reviewed and merged them. "
            "Slack is lower because decisions there are often informal and unreviewed. "
            "Commit messages are lowest because they are often terse and ambiguous."
        ),
        "alternatives": [
            "Equal confidence for all sources (loses signal)",
            "Manual confidence scoring per entry (too much friction)",
        ],
        "constraints": [
            "Never downgrade confidence below 0.50 for any source",
            "Manual entries always beat automated sources on conflict",
            "Confidence used in contradiction_check threshold (>0.75 = flag)",
        ],
        "made_by": "Varun, EVOKG research basis",
        "decision_type": "technical",
    },
    {
        "title": "Contradiction detection threshold",
        "content": (
            "A similarity score above 0.75 between a new decision and an existing "
            "decision triggers a contradiction warning. "
            "This decision is GOVERNED BY 'Confidence hierarchy: manual=0.95, pr=0.90, slack=0.65, commit=0.60'. "
            "This decision SUPPORTS 'Propose-validate-commit over Last-Write-Wins CRDT' "
            "by surfacing conflicts before they are committed. "
            "Never delete old decisions on contradiction — they form the compliance audit trail."
        ),
        "rationale": (
            "0.75 cosine similarity (all-MiniLM-L6-v2) catches semantically related "
            "decisions that likely conflict. Lower thresholds produce too many false positives. "
            "Higher thresholds miss real conflicts. The 0.75 value was calibrated on "
            "the smm-sync decision corpus where true contradictions cluster above 0.78."
        ),
        "alternatives": [
            "No contradiction detection (silent conflicts accumulate)",
            "LLM-based semantic contradiction check (expensive, adds latency)",
        ],
        "constraints": [
            "Threshold: 0.75 cosine similarity",
            "Old decisions MUST NOT be deleted — they are the audit trail",
            "Contradictions are noted in new episode body, never block the write",
        ],
        "made_by": "Varun, calibrated on smm-sync corpus",
        "decision_type": "technical",
    },
    # ── CAPTURE PIPELINE ─────────────────────────────────────────────────────
    {
        "title": "Passive GitHub capture over manual ADRs",
        "content": (
            "Capture decisions passively from GitHub PRs, commits, and issues "
            "rather than asking developers to write manual ADRs. "
            "This decision SUPERSEDES any plan for manual ADR tooling. "
            "This decision REQUIRES 'Two-stage Haiku/Sonnet pipeline for PR classification' "
            "to make per-PR processing affordable. "
            "This decision SUPPORTS 'Target buyer: VP Engineering not individual developers' "
            "because passive capture requires zero developer behaviour change. "
            "This decision is SUPPORTED BY 'All LLM calls: Haiku for classification, Sonnet for extraction'."
        ),
        "rationale": (
            "Manual ADRs have less than 10% adoption rate across engineering teams. "
            "Developers write ADRs when they remember, when they have time, and when "
            "the decision feels important enough. This means 90%+ of decisions are "
            "never captured. Passive GitHub capture gets 100% coverage automatically."
        ),
        "alternatives": [
            "Manual ADR workflow with templates (SUPERSEDED — <10% adoption rate)",
            "Slack bot that prompts for decisions after deployments",
            "Weekly decision review meetings",
        ],
        "constraints": [
            "All capture must be passive — no manual input required",
            "GitHub webhook or polling integration required",
            "PR classification must stay under $0.01 per PR",
        ],
        "made_by": "Varun, adoption rate research",
        "decision_type": "product",
    },
    {
        "title": "Two-stage Haiku/Sonnet pipeline for PR classification",
        "content": (
            "GitHub PR processing uses two stages: "
            "Stage 1 — Haiku classifies whether a PR contains an architectural decision (fast, cheap). "
            "Stage 2 — Sonnet extracts structured decision entities from classified PRs (slow, accurate). "
            "This pipeline is REQUIRED BY 'Passive GitHub capture over manual ADRs'. "
            "This pipeline SUPPORTS 'Keep API costs under $0.01 per PR'. "
            "This pipeline USES 'All LLM calls: Haiku for classification, Sonnet for extraction'."
        ),
        "rationale": (
            "80-90% of PRs contain no architectural decisions (dependency bumps, bug fixes, "
            "style changes). Running Sonnet on every PR would be 10x more expensive than needed. "
            "Haiku filters the 80-90% noise at minimal cost; Sonnet only processes the 10-20% "
            "signal. This keeps total pipeline cost under $0.01/PR."
        ),
        "alternatives": [
            "Sonnet for all stages (10x more expensive)",
            "Rule-based classifier only, no LLM (misses complex decisions)",
            "Single-stage extraction with Haiku only (lower accuracy)",
        ],
        "constraints": [
            "Stage 1 (classification) must use Haiku only",
            "Stage 2 (extraction) may use Sonnet",
            "Classification result must be cached — never reclassify the same PR",
        ],
        "made_by": "Varun, cost-accuracy trade-off analysis",
        "decision_type": "technical",
    },
    {
        "title": "stdout isolation: all prints to stderr during MCP stdio",
        "content": (
            "During MCP server operation over stdio transport, all debug prints and "
            "log messages must go to stderr, never stdout. "
            "This decision FIXES 'MCP protocol corruption from debug prints'. "
            "This decision SUPPORTS 'MCP server over REST API for IDE integration' "
            "by ensuring the stdio transport remains clean. "
            "This is a hard constraint — any print() to stdout corrupts the JSON-RPC stream."
        ),
        "rationale": (
            "MCP uses JSON-RPC over stdio. Any byte written to stdout that is not "
            "a valid JSON-RPC message corrupts the protocol stream and breaks the "
            "client connection. A single debug print() would cause silent tool failures "
            "that are extremely difficult to diagnose. Routing all output to stderr "
            "is the only safe approach."
        ),
        "alternatives": [
            "Disable all logging in MCP mode (loses observability)",
            "File-based logging only (complex setup)",
        ],
        "constraints": [
            "HARD RULE: Never write to stdout during MCP server operation",
            "All print() calls must use file=sys.stderr",
            "Logger handlers must route to stderr in MCP mode",
        ],
        "made_by": "Varun, debugging MCP protocol corruption",
        "decision_type": "technical",
    },
    {
        "title": "MCP protocol corruption from debug prints",
        "content": (
            "Bug: Debug print() statements to stdout corrupt the MCP JSON-RPC protocol stream. "
            "This bug was FIXED BY 'stdout isolation: all prints to stderr during MCP stdio'. "
            "Root cause: MCP stdio transport reads stdout as JSON-RPC; any non-JSON bytes "
            "cause the client to fail silently or crash. "
            "This bug MOTIVATED 'MCP server over REST API for IDE integration' to also "
            "consider HTTP transport as a fallback."
        ),
        "rationale": (
            "Discovered during integration testing of MCP tools with Claude Code. "
            "Tool calls were returning malformed responses. Root cause traced to "
            "a debug print() in the graph client that wrote to stdout. "
            "The MCP client received the debug output as part of the JSON-RPC response."
        ),
        "alternatives": [
            "Switch to HTTP transport (loses zero-config advantage)",
        ],
        "constraints": [
            "This bug is permanently fixed by the stderr isolation decision",
            "Any new code must never write to stdout during MCP operation",
        ],
        "made_by": "Varun, debugging session",
        "decision_type": "technical",
    },
    # ── ARCHITECTURE ─────────────────────────────────────────────────────────
    {
        "title": "MCP server over REST API for IDE integration",
        "content": (
            "Use MCP (Model Context Protocol) server over stdio as the primary IDE "
            "integration mechanism, not a REST API or VS Code extension. "
            "This decision ENABLES 'Zero-config developer onboarding'. "
            "This decision is PREFERRED OVER 'VS Code extension approach' "
            "because VS Code extensions create vendor lock-in. "
            "This decision REQUIRES 'stdout isolation: all prints to stderr during MCP stdio' "
            "to prevent protocol corruption. "
            "This decision SUPPORTS 'Vendor-neutral context layer — serve Claude Code AND Cursor'."
        ),
        "rationale": (
            "MCP is now natively supported by Claude Code, Cursor, and GitHub Copilot. "
            "A single MCP server serves all three without IDE-specific code. "
            "VS Code extensions require maintenance per IDE version and create "
            "vendor lock-in. REST APIs require developers to configure endpoints. "
            "MCP over stdio is zero-configuration: point at the binary, done."
        ),
        "alternatives": [
            "VS Code extension (PREFERRED OVER — vendor lock-in)",
            "REST API endpoint (requires developer configuration)",
            "Language Server Protocol (LSP) extension",
        ],
        "constraints": [
            "MCP is the only distribution protocol — no IDE plugins",
            "stdio transport is primary; HTTP transport is optional fallback",
            "All MCP tool responses must be valid JSON-RPC",
        ],
        "made_by": "Varun, distribution strategy",
        "decision_type": "architectural",
    },
    {
        "title": "Zero-config developer onboarding",
        "content": (
            "Developer onboarding to Axiom Hub must require zero configuration steps "
            "beyond installing the package and setting ANTHROPIC_API_KEY. "
            "This is ENABLED BY 'MCP server over REST API for IDE integration' "
            "because MCP stdio requires no server setup. "
            "This is ENABLED BY 'Run 100% locally without Docker' "
            "because no Docker setup is required. "
            "This is ENABLED BY 'Use Kuzu as embedded graph DB for development' "
            "because no database server is needed. "
            "This goal SUPPORTS 'Target buyer: VP Engineering not individual developers' "
            "because platform teams need tools that developers will actually adopt."
        ),
        "rationale": (
            "Every configuration step kills adoption by 30-50%. "
            "If onboarding requires: install Docker, start containers, configure endpoints, "
            "set up database — most developers abandon before getting to value. "
            "pip install + ANTHROPIC_API_KEY + smm init is the entire setup. "
            "The MCP config is a single JSON snippet that takes 30 seconds to add."
        ),
        "alternatives": [
            "Docker Compose for full stack (SUPERSEDED by zero-config goal)",
            "Cloud-hosted service (adds auth/network complexity)",
        ],
        "constraints": [
            "Setup must complete in under 5 minutes",
            "No Docker requirement",
            "No database server requirement",
            "Single API key (ANTHROPIC_API_KEY) is the only external dependency for writes",
        ],
        "made_by": "Varun, developer experience principle",
        "decision_type": "product",
    },
    {
        "title": "Propose-validate-commit over Last-Write-Wins CRDT",
        "content": (
            "State mutations go through propose → validate → commit sequence. "
            "Last-Write-Wins CRDT was REJECTED and SUPERSEDED BY this approach. "
            "This decision is SUPPORTED BY 'Contradiction detection threshold' "
            "which surfaces conflicts in the validate stage. "
            "This decision SUPPORTS 'os.rename() for atomic file locking' "
            "because atomic rename is the implementation mechanism for the commit step."
        ),
        "rationale": (
            "LWW CRDT causes silent failures in agentic workflows. If Agent A spends "
            "5 minutes on complex static analysis and writes at t=100, then Agent B "
            "fixes a typo at t=105, LWW silently discards Agent A's work. This destroys "
            "trust in the coordination layer. Propose-validate-commit surfaces conflicts "
            "explicitly."
        ),
        "alternatives": [
            "Last-Write-Wins CRDT (SUPERSEDED — causes silent data loss)",
            "Operational Transformation (too complex for file coordination)",
            "Gossip protocols (overkill for single-machine use case)",
        ],
        "constraints": [
            "No silent overwrites ever",
            "Every rejected write must return an explicit error",
            "Event log must be append-only",
        ],
        "made_by": "Varun + GPT CPO review",
        "decision_type": "architectural",
    },
    {
        "title": "os.rename() for atomic file locking",
        "content": (
            "File claims use POSIX-atomic os.rename() operations stored in "
            ".smm/locks/ directory. "
            "This decision IMPLEMENTS 'Propose-validate-commit over Last-Write-Wins CRDT' "
            "as the commit mechanism. "
            "This decision SUPPORTS 'Run 100% locally without Docker' "
            "because it requires no external lock server. "
            "This decision CONSTRAINS the file coordination system to POSIX filesystems only."
        ),
        "rationale": (
            "os.rename() is atomic on POSIX systems (macOS and Linux). If two agents "
            "try to claim the same file simultaneously, only one rename succeeds — the "
            "OS resolves the race condition at the kernel level. The other agent gets "
            "FileNotFoundError immediately. Zero-overhead, no server required, works offline."
        ),
        "alternatives": [
            "SQLite ACID locking (heavier dependency)",
            "Redis distributed locks (requires server, SUPERSEDED by local-only goal)",
            "File-based mutex with lockfile (not atomic)",
        ],
        "constraints": [
            "Must work on macOS and Linux (POSIX only)",
            "No network dependency for basic locking",
            "Lock files live in .smm/locks/",
            "Do not use os.rename() across filesystems — atomic only within same filesystem",
        ],
        "made_by": "Varun, validated by distributed systems research",
        "decision_type": "technical",
    },
    # ── PRODUCT ──────────────────────────────────────────────────────────────
    {
        "title": "Target buyer: VP Engineering not individual developers",
        "content": (
            "Axiom Hub is an enterprise infrastructure product sold to VP Engineering, "
            "CTO, and Platform Engineering Leads — not individual developers. "
            "This decision CONTRADICTS 'Build CLI-first for developers' "
            "but is RESOLVED BY supporting both (CLI for adoption, dashboard for monetisation). "
            "This decision SUPPORTS 'Dashboard is the monetisation layer'. "
            "This decision is SUPPORTED BY 'Never expose raw MCP to enterprise customers' "
            "because enterprise security compliance is a VP Engineering concern. "
            "This decision SUPPORTS 'Zero-config developer onboarding' "
            "because VP buyers need tools developers will actually adopt."
        ),
        "rationale": (
            "Individual developers will not pay for context management. They abandon "
            "tools that add friction. The true buyer is the person managing the systemic "
            "fallout of vibe coding cleanup, watching AI-generated technical debt "
            "accumulate, and needing governance, auditability, and predictability. "
            "Platform fee: $10-50K/year. Per-seat: $30-50/user/month."
        ),
        "alternatives": [
            "Bottom-up individual developer tool, freemium (contradicts enterprise focus)",
            "Open source only with no monetisation",
            "Usage-based per-token pricing (unpredictable for enterprise)",
        ],
        "constraints": [
            "Product must never require developer to change coding flow",
            "All capture must be passive — no manual input",
            "Enterprise features: SSO, audit trails, RBAC are required",
        ],
        "made_by": "Varun + Gemini competitive research",
        "decision_type": "product",
    },
    {
        "title": "Never expose raw MCP to enterprise customers",
        "content": (
            "Raw MCP has 6 fatal security flaws. A governed MCP gateway is required "
            "before any enterprise deployment. "
            "This decision REQUIRES 'Build gateway/proxy layer for MCP security'. "
            "This decision PROTECTS 'Enterprise security compliance'. "
            "This decision SUPPORTS 'Target buyer: VP Engineering not individual developers' "
            "because VP Engineering buyers will not accept raw MCP security risks. "
            "This decision CONSTRAINS 'MCP server over REST API for IDE integration' "
            "to include a security gateway layer before enterprise exposure."
        ),
        "rationale": (
            "Security analysis identified 6 fatal flaws in raw MCP: "
            "no enforced authentication, session IDs exposed in URLs, "
            "prompt injection via tool descriptions, no risk-tiering for tools, "
            "ambiguous identity management, unstructured tool responses. "
            "Enterprises cannot accept these risks. The gateway enforces RBAC, "
            "validates payloads, categorises tools by risk level, and provides audit trails."
        ),
        "alternatives": [
            "Ship raw MCP and address security later (unacceptable for enterprise)",
            "Use only read-only MCP tools initially (limits product value)",
        ],
        "constraints": [
            "DO NOT expose raw MCP to enterprise customers",
            "Gateway must enforce RBAC on all tool calls",
            "All MCP calls must be logged for audit trail",
            "Prompt injection prevention is non-negotiable",
        ],
        "made_by": "Varun, based on Scalifi AI security analysis",
        "decision_type": "architectural",
    },
    {
        "title": "AGENTS.md as source of truth over smm.toml",
        "content": (
            "We SUPERSEDE the smm.toml compilation format in favour of ingesting AGENTS.md natively. "
            "AGENTS.md is the source of truth for project context. "
            "This decision SUPPORTS 'Vendor-neutral context layer — serve Claude Code AND Cursor' "
            "because AGENTS.md is natively read by both Claude Code and Cursor. "
            "This decision SUPERSEDES 'smm.toml with Jinja2 compilation approach'. "
            "This decision SUPPORTS 'Zero-config developer onboarding' "
            "because no compilation step is required — edit AGENTS.md directly."
        ),
        "rationale": (
            "AGENTS.md has already won the format war — 60,000+ GitHub repos use it. "
            "Cursor officially deprecated .cursorrules in favour of it. "
            "Building a pre-compiler for a format that already standardised would create "
            "unnecessary friction for developers. Native ingestion is simpler and "
            "immediately compatible with every AI coding tool that reads AGENTS.md."
        ),
        "alternatives": [
            "smm.toml with Jinja2 compilation (SUPERSEDED — format has not won)",
            "Custom JSON schema",
            "YAML configuration",
        ],
        "constraints": [
            "AGENTS.md must be the single source of truth",
            "No smm.toml should be generated or required",
            "Must parse AGENTS.md sections: Architecture, Constraints, Danger Zones, Modules",
        ],
        "made_by": "Varun + CPO review",
        "decision_type": "architectural",
    },
]


async def seed_test_data(client: GraphClient, project: str = "smm-sync") -> None:
    """Seed the graph with 18 interconnected Axiom Hub decisions.

    Each episode body explicitly names other decisions using relationship
    keywords (SUPERSEDES, REQUIRES, ENABLES, CONTRADICTS, PREFERRED_OVER,
    SUPPORTS, CONSTRAINS, FIXES, GOVERNED BY) so Graphiti's LLM entity
    extraction creates Entity nodes AND edges between them.

    Makes approximately 54-126 Anthropic API calls. Takes 5-10 minutes.
    Prints progress for each decision.

    Args:
        client: Initialised GraphClient instance.
        project: Project name to seed decisions into.
    """
    total = len(SEED_DECISIONS)
    start_time = time.time()

    print(f"\nSeeding {total} interconnected decisions for project: {project}")
    print("Each episode body cross-references other decisions so Graphiti")
    print("creates Entity nodes AND edges — not isolated circles.")
    print(f"This will make ~{total * 3}-{total * 7} Anthropic API calls and take 5-10 minutes.\n")

    for i, decision in enumerate(SEED_DECISIONS, 1):
        short_title = decision["title"][:60]
        print(f"Seeding decision {i}/{total}: {short_title}...")
        try:
            await client.add_decision(
                title=decision["title"],
                content=decision["content"],
                rationale=decision["rationale"],
                made_by=decision["made_by"],
                project=project,
                constraints=decision.get("constraints", []),
                alternatives=decision.get("alternatives", []),
                decision_type=decision.get("decision_type", "technical"),
            )
            print(f"  ✓ Done")
        except Exception as e:
            print(f"  ✗ Failed: {e}")

    elapsed = time.time() - start_time
    minutes = int(elapsed // 60)
    seconds = int(elapsed % 60)
    print(f"\nSeeded {total} decisions for project: {project} (took {minutes}m {seconds}s)")
    print("\nGraphiti should now have Entity nodes connected by edges for:")
    print("  SUPERSEDES, REQUIRES, ENABLES, SUPPORTS, CONSTRAINS,")
    print("  PREFERRED_OVER, CONTRADICTS, FIXES, GOVERNED_BY, IMPLEMENTS")
    print("\nOpen /graph to see the connected decision tree.")
