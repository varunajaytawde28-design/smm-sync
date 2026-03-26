"""Seed the context graph with decisions extracted from four real PRDs.

PRDs covered:
  1. Slack Passive Capture Connector
  2. Zero-Config Repository Onboarding (smm setup)
  3. Multi-Repository Context Federation
  4. CaaS Pricing and Packaging

Usage:
    cd /Users/varunajaytawde/projects/smm-sync
    export ANTHROPIC_API_KEY=sk-ant-...
    python scripts/seed_prd_test.py

WARNING: Each add_decision call makes ~3-7 Anthropic API calls (Graphiti).
Seeding ~20 decisions takes 5-15 minutes.
"""
from __future__ import annotations

import asyncio
import os
import sys
import time
from pathlib import Path

# Make sure smm_sync is importable when run from project root
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from smm_sync.context_graph.client import GraphClient

PROJECT = "smm-sync"

DECISIONS = [
    # -------------------------------------------------------------------------
    # PRD 1: Slack Passive Capture Connector
    # -------------------------------------------------------------------------
    {
        "title": "Use Slack Events API (not RTM) for passive capture",
        "content": (
            "The Slack connector uses Slack Events API with a webhook endpoint at "
            "POST /slack/events (added to the FastAPI dashboard server). "
            "RTM (Real Time Messaging API) was rejected as Slack deprecated it in 2023."
        ),
        "rationale": (
            "RTM is deprecated by Slack as of 2023 and is being removed. "
            "It is unreliable and no longer supported for new integrations. "
            "Events API is the current supported approach for passive listening."
        ),
        "alternatives": [
            "Slack RTM (Real Time Messaging API) — deprecated by Slack 2023, being removed",
            "Polling Slack search API — rate limited to 1 req/min, misses edits/threads",
            "Zapier/Make.com integration — external dependency, sends data to third party, violates local-first",
        ],
        "constraints": [
            "SLACK_BOT_TOKEN must be set in environment",
            "Bot must be explicitly invited to each channel before it can listen",
            "Never capture #general or #random without explicit configuration",
            "Slack message IDs must be stored to prevent duplicate capture on reconnect",
        ],
        "made_by": "Varun (TPM)",
        "decision_type": "technical",
        "source_type": "manual",
    },
    {
        "title": "Slack capture confidence is 0.65 (lower than PR at 0.90)",
        "content": (
            "Decisions captured from Slack are assigned a confidence score of 0.65 "
            "in the source confidence hierarchy. This is lower than GitHub PR (0.90) "
            "because Slack is informal conversation, not a reviewed decision record."
        ),
        "rationale": (
            "70% of architectural decisions happen in Slack before reaching a PR. "
            "By the time a decision is in a PR it has been debated and consensus reached. "
            "Slack captures the debate, but the signal is noisier and less authoritative "
            "than a merged PR reviewed by the team."
        ),
        "alternatives": [],
        "constraints": [
            "source_type='slack' must be set when writing Slack decisions to graph",
            "Confidence hierarchy: manual=0.95, github_pr=0.90, slack=0.65",
        ],
        "made_by": "Varun (TPM)",
        "decision_type": "architectural",
        "source_type": "manual",
    },
    {
        "title": "Two-stage Haiku classifier + Sonnet extractor for Slack messages",
        "content": (
            "Slack message processing uses the same two-stage extraction pipeline as "
            "GitHub capture: Haiku classifier first (is this a decision signal?), "
            "then Sonnet extractor for full decision record. "
            "Minimum message length for classification: 100 characters."
        ),
        "rationale": (
            "Below 100 characters, Haiku classifies as non-decision. Short messages "
            "lack the context needed for meaningful decision extraction. "
            "Two-stage pipeline keeps costs low: Haiku filters most messages, "
            "Sonnet only runs on confirmed decision signals."
        ),
        "alternatives": [],
        "constraints": [
            "Minimum message length: 100 characters (below this = non-decision)",
            "Thread context: capture full thread when decision detected, not just trigger message",
            "Store Slack user ID as author — never store actual names or emails (privacy)",
        ],
        "made_by": "Varun (TPM)",
        "decision_type": "technical",
        "source_type": "manual",
    },
    {
        "title": "Slack capture scope: designated channels only, no DMs or private channels",
        "content": (
            "The Slack connector only listens on a configurable list of explicitly "
            "designated channels. DMs and private channels are never captured without "
            "explicit per-channel opt-in."
        ),
        "rationale": (
            "Capturing DMs or private channels without consent violates privacy expectations "
            "and could expose sensitive personal communications. Explicit channel opt-in "
            "gives teams control over what is captured."
        ),
        "alternatives": [],
        "constraints": [
            "Never capture DMs or private channels without explicit opt-in per channel",
            "Never capture #general or #random without explicit configuration",
            "Bot must be invited to channels before it can listen",
        ],
        "made_by": "Varun (TPM)",
        "decision_type": "product",
        "source_type": "manual",
    },

    # -------------------------------------------------------------------------
    # PRD 2: Zero-Config Repository Onboarding (smm setup)
    # -------------------------------------------------------------------------
    {
        "title": "smm setup: single interactive command for zero-config onboarding",
        "content": (
            "Build smm setup — a single interactive wizard that handles all onboarding. "
            "Replaces the current 6-step manual process. "
            "Target: developer sees first decisions in < 60 seconds from running smm setup."
        ),
        "rationale": (
            "Current onboarding requires 6 steps (smm init, edit github.yml, export keys, "
            "smm capture run, smm serve). Developers abandon tools requiring more than "
            "3 steps to see value. The first session must show value within 60 seconds."
        ),
        "alternatives": [
            "GUI installer (.dmg/.exe) — too heavy, wrong audience (developers live in terminal)",
            "Auto-start on system boot (LaunchAgent/systemd) — too invasive, raises security concerns",
            "Browser-based OAuth flow for GitHub — requires local web server for callback, too complex",
        ],
        "constraints": [
            "smm setup must be idempotent — running twice must not duplicate anything",
            "Must work on macOS, Linux, Windows WSL",
            "If .smm/ already exists, prompt before overwriting",
            "ANTHROPIC_API_KEY and GITHUB_TOKEN must never be written to any file — environment only",
        ],
        "made_by": "Varun (TPM)",
        "decision_type": "product",
        "source_type": "manual",
    },
    {
        "title": "smm setup wizard: 7-step automated sequence",
        "content": (
            "The smm setup wizard executes this exact sequence: "
            "1. Detects git remote automatically (no manual input). "
            "2. Generates .smm/github.yml from detected remote. "
            "3. Checks for API keys — if missing, gives exact commands to get them. "
            "4. Runs smm capture run --once automatically. "
            "5. Prints how many decisions were found. "
            "6. Generates ONBOARDING.md automatically. "
            "7. Starts MCP server and prints the .mcp.json snippet."
        ),
        "rationale": (
            "Each step removes one manual action from the developer. "
            "Auto-detection of git remote eliminates the most error-prone step. "
            "Automatic initial capture ensures developer sees value immediately. "
            "The .mcp.json snippet is the final step to plug into their AI agent."
        ),
        "alternatives": [],
        "constraints": [
            "smm setup must be idempotent — running twice must not duplicate",
            "ANTHROPIC_API_KEY and GITHUB_TOKEN must never be written to any file",
        ],
        "made_by": "Varun (TPM)",
        "decision_type": "technical",
        "source_type": "manual",
    },

    # -------------------------------------------------------------------------
    # PRD 3: Multi-Repository Context Federation
    # -------------------------------------------------------------------------
    {
        "title": "Global ~/.caas/ directory for multi-repo context federation",
        "content": (
            "Support a global CaaS directory at ~/.caas/ that aggregates decisions from "
            "multiple repos with configurable access control per repo. "
            "Per-repo .smm/ directories still work for local-only projects. "
            "Global mode: smm --global serves all configured repos."
        ),
        "rationale": (
            "Engineering teams work across multiple repos. A decision in backend affects "
            "frontend. A constraint in shared-utils affects every team. Per-project "
            ".smm/ isolation means context never crosses repo boundaries, forcing teams "
            "with 5+ repos to run CaaS separately for each one."
        ),
        "alternatives": [
            "Central cloud sync — violates local-first principle, non-starter for enterprise security",
            "Symlinks between .smm/ directories — fragile, breaks on repo moves, confusing DX",
            "Monorepo-only solution (single .smm/ at root) — excludes majority polyrepo enterprise teams",
        ],
        "constraints": [
            "Global mode is opt-in — default is still local .smm/",
            "Circular dependencies between repos must be detected and refused",
            "Each repo's decisions retain their source repo tag",
            "Team visibility requires repos to share the same GitHub org — cross-org federation out of scope",
        ],
        "made_by": "Varun (TPM)",
        "decision_type": "architectural",
        "source_type": "manual",
    },
    {
        "title": "Three-tier repo visibility: private, team, public",
        "content": (
            "Repos in global mode are tagged with one of three visibility levels: "
            "private (only visible to that repo's agents), "
            "team (visible to all repos in the same org), "
            "public (visible to all configured repos). "
            "MCP server in global mode exposes decisions filtered by visibility rules."
        ),
        "rationale": (
            "Different repos have different sensitivity levels. Infrastructure secrets "
            "in a private repo should not be visible to frontend repos. Team-wide "
            "architecture decisions should be visible across the org. "
            "Three tiers mirrors standard GitHub repo visibility."
        ),
        "alternatives": [],
        "constraints": [
            "Circular repo dependencies must be detected and refused (A->B->A = error)",
            "Cross-org federation is explicitly out of scope",
            "Team visibility requires same GitHub org",
        ],
        "made_by": "Varun (TPM)",
        "decision_type": "architectural",
        "source_type": "manual",
    },

    # -------------------------------------------------------------------------
    # PRD 4: CaaS Pricing and Packaging
    # -------------------------------------------------------------------------
    {
        "title": "Three-tier pricing: Solo free, Team $20/user/month, Enterprise $35+$499",
        "content": (
            "CaaS uses a three-tier pricing model: "
            "Solo: Free forever (1 user, 1 repo, local only). "
            "Team: $20/user/month (unlimited repos, Slack capture). "
            "Enterprise: $35/user/month + $499/month platform fee (SSO, RBAC, audit export, SLA)."
        ),
        "rationale": (
            "Individual devs expense $50-100/month, small teams $500-2000/month, "
            "enterprise $50K+ annually. Free tier drives adoption and Show HN virality. "
            "$20/user undercuts HAM ($999/month flat). $35/user + platform fee matches "
            "GitHub Copilot Enterprise pricing, making it an easy budget comparison. "
            "Platform fee covers fixed infrastructure costs regardless of query volume."
        ),
        "alternatives": [
            "Usage-based only (per query/per decision) — creates billing anxiety, kills usage",
            "Freemium with feature limits — community research shows devs resent artificial caps",
            "One-time license — no recurring revenue, cannot fund infrastructure long-term",
            "Per-repo pricing — penalizes polyrepo teams, the exact customers we want most",
        ],
        "constraints": [
            "Free tier must be genuinely useful, not crippled — unlimited decisions from 1 repo",
            "No credit card required for free tier ever",
            "Enterprise pricing is annual billing only",
            "Team tier is monthly or annual (annual = 2 months free)",
            "No per-seat minimums on Team tier (2-person startups must be able to afford it)",
        ],
        "made_by": "Varun (TPM)",
        "decision_type": "product",
        "source_type": "manual",
    },
    {
        "title": "Slack capture is a Team tier feature — not available on Solo free tier",
        "content": (
            "Slack Passive Capture Connector is available on Team ($20/user/month) and "
            "Enterprise tiers only. Solo free tier is limited to 1 repo, local capture only."
        ),
        "rationale": (
            "Slack integration requires server-side webhook infrastructure and ongoing "
            "polling costs. These fixed costs are covered by the $499/month platform fee "
            "at Enterprise and bundled into the Team tier. "
            "Free tier captures unlimited decisions but only from 1 local repo — "
            "this is still genuinely useful and not crippled."
        ),
        "alternatives": [],
        "constraints": [
            "Free tier: 1 user, 1 repo, local only — no Slack, no multi-repo",
            "Team tier: unlimited repos, Slack capture included",
            "Enterprise: SSO, RBAC, audit export, SLA — on top of Team features",
        ],
        "made_by": "Varun (TPM)",
        "decision_type": "product",
        "source_type": "manual",
    },
    {
        "title": "No per-seat minimums on any tier — 2-person startups must afford Team",
        "content": (
            "All pricing tiers have zero per-seat minimums. A 2-person startup can "
            "subscribe to Team at $40/month total. Enterprise platform fee ($499/month) "
            "applies regardless of seat count."
        ),
        "rationale": (
            "Per-seat minimums exclude small teams who are early adopters and word-of-mouth "
            "drivers. A 2-person startup paying $40/month today becomes a 50-person company "
            "paying $1000/month in two years. Locking them out early destroys long-term LTV."
        ),
        "alternatives": [],
        "constraints": [
            "No per-seat minimums on Team tier",
            "Enterprise $499/month platform fee is fixed regardless of seat count",
            "Annual Enterprise billing only — no monthly Enterprise option",
        ],
        "made_by": "Varun (TPM)",
        "decision_type": "product",
        "source_type": "manual",
    },
]


async def main() -> None:
    """Seed graph with PRD decisions and print progress."""
    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not api_key:
        print("ERROR: ANTHROPIC_API_KEY is not set.", file=sys.stderr)
        print("Run: export ANTHROPIC_API_KEY=sk-ant-...", file=sys.stderr)
        sys.exit(1)

    project_root = Path(__file__).parent.parent
    graph_dir = project_root / ".smm" / "graph"
    graph_dir.parent.mkdir(parents=True, exist_ok=True)

    client = GraphClient(graph_dir=graph_dir, api_key=api_key)

    total = len(DECISIONS)
    start = time.time()
    print(f"\nSeeding {total} PRD decisions into project '{PROJECT}'")
    print("Each decision makes ~3-7 Anthropic API calls (Graphiti entity extraction).")
    print("Expected time: 5-15 minutes.\n")

    succeeded = 0
    for i, d in enumerate(DECISIONS, 1):
        short = d["title"][:65]
        print(f"[{i:02d}/{total}] {short}...")
        try:
            uuid = await client.add_decision(
                title=d["title"],
                content=d["content"],
                rationale=d["rationale"],
                made_by=d["made_by"],
                project=PROJECT,
                constraints=d.get("constraints", []),
                alternatives=d.get("alternatives", []),
                decision_type=d.get("decision_type", "technical"),
                source_type=d.get("source_type", "manual"),
            )
            print(f"       OK  {uuid[:8]}")
            succeeded += 1
        except Exception as exc:
            print(f"       FAILED: {exc}")

    elapsed = time.time() - start
    print(f"\n{succeeded}/{total} decisions seeded ({int(elapsed//60)}m {int(elapsed%60)}s)")

    if succeeded < total:
        print(f"\nWARNING: {total - succeeded} decisions failed to seed.", file=sys.stderr)
        sys.exit(1)
    else:
        print("\nSeed complete. Run verification queries:")
        print('  smm query "why did we reject RTM API"')
        print('  smm query "what is the pricing for team tier"')
        print('  smm query "how does multi-repo federation work"')
        print('  smm query "what are the constraints for slack connector"')


if __name__ == "__main__":
    asyncio.run(main())
