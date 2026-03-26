"""
CaaS Demo Data Seeder — Zero API Cost

Injects realistic fake data directly into the local
graph and log files without any LLM API calls.

Run: python scripts/seed_demo_data.py

What it seeds:
- 12 decisions (mix of types, sources, confidence)
- 3 contradictions
- 20 compliance log entries
- 3 pending PR decisions for the feed
- 3 board items
"""

import asyncio
import json
import sys
import os
from pathlib import Path
from datetime import datetime, timezone, timedelta
import uuid

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

SMM_DIR = Path(__file__).parent.parent / ".smm"
SMM_DIR.mkdir(parents=True, exist_ok=True)

DECISIONS = [
    {"title": "Propose-validate-commit over LWW CRDT", "type": "architectural", "confidence": 0.95, "source_type": "manual", "source_pr": None, "rationale": "LWW CRDT silently discards Agent A work when Agent B writes at a later timestamp. Destroys trust in coordination layer.", "is_constraint": False, "days_ago": 5, "status": "approved"},
    {"title": "Never expose raw MCP to enterprise customers", "type": "architectural", "confidence": 0.95, "source_type": "manual", "source_pr": None, "rationale": "6 fatal MCP security flaws. Gateway required before any enterprise deployment.", "is_constraint": True, "days_ago": 5, "status": "approved"},
    {"title": "Build on Graphiti over custom temporal graph", "type": "architectural", "confidence": 0.95, "source_type": "manual", "source_pr": None, "rationale": "Saves 25-37 weeks vs building from scratch. Moat is not in the graph layer.", "is_constraint": False, "days_ago": 5, "status": "approved"},
    {"title": "Use Kuzu as embedded graph DB for development", "type": "technical", "confidence": 0.80, "source_type": "manual", "source_pr": None, "rationale": "macOS 13 blocks Docker Desktop. Kuzu is embedded, no server needed. FalkorDB is production target.", "is_constraint": False, "days_ago": 4, "status": "approved"},
    {"title": "FalkorDB via Docker for local development", "type": "technical", "confidence": 0.60, "source_type": "github_commit", "source_pr": None, "rationale": "Original plan. Blocked by macOS 13 constraint.", "is_constraint": False, "days_ago": 7, "status": "superseded"},
    {"title": "Two-stage DRMiner extraction: Haiku then Sonnet", "type": "technical", "confidence": 0.90, "source_type": "github_pr", "source_pr": "47", "rationale": "DRMiner ICSE 2024: F1 0.65 vs 0.58 single-shot. 14x downstream improvement.", "is_constraint": False, "days_ago": 2, "status": "approved"},
    {"title": "Vendor neutrality — serve Claude Code, Cursor, Copilot", "type": "product", "confidence": 0.95, "source_type": "manual", "source_pr": None, "rationale": "Platforms will not build cross-tool coordination. Vendor neutrality is the core moat.", "is_constraint": True, "days_ago": 5, "status": "approved"},
    {"title": "Integration depth is the primary moat", "type": "product", "confidence": 0.85, "source_type": "manual", "source_pr": None, "rationale": "ProfitWell: each integration adds 10% better retention. Datadog built 1000+ integrations as moat.", "is_constraint": False, "days_ago": 3, "status": "approved"},
    {"title": "Target buyer: VP Engineering not individual developers", "type": "product", "confidence": 0.90, "source_type": "manual", "source_pr": None, "rationale": "Individual devs do not pay for context management. VP Eng pays for governance and AI ROI.", "is_constraint": False, "days_ago": 6, "status": "approved"},
    {"title": "All LLM calls: Haiku for classification, Sonnet for extraction", "type": "technical", "confidence": 0.95, "source_type": "manual", "source_pr": None, "rationale": "Cost efficiency. Haiku costs 10x less. Classification is binary — does not need Sonnet.", "is_constraint": True, "days_ago": 3, "status": "approved"},
    {"title": "AGENTS.md as source of truth over smm.toml", "type": "architectural", "confidence": 0.95, "source_type": "manual", "source_pr": None, "rationale": "AGENTS.md adopted by 60000+ GitHub repos. Claude Code and Cursor both read it natively.", "is_constraint": False, "days_ago": 8, "status": "approved"},
    {"title": "Workflow boundary context surfacing only", "type": "architectural", "confidence": 0.88, "source_type": "github_pr", "source_pr": "51", "rationale": "ProAIDE field study: 52% engagement at boundaries vs 62% dismissal mid-task. p=0.0016.", "is_constraint": False, "days_ago": 1, "status": "approved"},
]

PENDING_DECISIONS = [
    {"title": "Add Slack Events API connector", "type": "architectural", "confidence": 0.88, "source_type": "github_pr", "source_pr": "52", "author": "varunajaytawde", "summary": "Passive Slack capture using Events API. Same two-stage extraction as GitHub capture.", "diff": [["add", "class SlackCapture(BaseCapture):"], ["add", '    """Passive Slack decision capture."""'], ["neutral", "    # Events API, not RTM (deprecated)"]], "deja_vu": None, "minutes_ago": 8},
    {"title": "Switch state management to LWW CRDT", "type": "architectural", "confidence": 0.70, "source_type": "github_pr", "source_pr": "53", "author": "dev-2", "summary": "Proposes LWW CRDT for concurrent agent state updates. Claims simpler than propose-validate-commit.", "diff": [["remove", "async def propose(state, change):"], ["add", "def apply_lww(state, change, timestamp):"], ["add", "    state[change.key] = change.value"]], "deja_vu": "LWW CRDT was explicitly rejected in Decision #1 — silently discards work.", "minutes_ago": 47},
    {"title": "smm install — zero-config onboarding wizard", "type": "technical", "confidence": 0.92, "source_type": "github_pr", "source_pr": "54", "author": "varunajaytawde", "summary": "Single interactive command for full CaaS onboarding. Validates keys, detects remote, runs capture.", "diff": [["add", "@cli.command('install')"], ["add", "def install():"], ["neutral", "    # Prompts keys, validates, runs capture"]], "deja_vu": None, "minutes_ago": 120},
]

CONTRADICTIONS = [
    {"id": str(uuid.uuid4()), "decision_a": "FalkorDB via Docker for development", "decision_b": "Use Kuzu as embedded graph DB", "explanation": "Dev database changed from FalkorDB to Kuzu. Old decision not marked superseded.", "detected_at": (datetime.now(timezone.utc) - timedelta(days=1)).isoformat(), "resolved": False},
    {"id": str(uuid.uuid4()), "decision_a": "PR context injection at workflow boundaries", "decision_b": "Context surfacing on-demand only", "explanation": "ProAIDE boundary injection conflicts with earlier on-demand-only decision.", "detected_at": (datetime.now(timezone.utc) - timedelta(hours=12)).isoformat(), "resolved": False},
    {"id": str(uuid.uuid4()), "decision_a": "Vendor neutrality constraint", "decision_b": "Claude Code prioritized in MCP design", "explanation": "Tension between vendor neutrality and Claude-Code-first MCP tool design.", "detected_at": (datetime.now(timezone.utc) - timedelta(hours=6)).isoformat(), "resolved": False},
]

BOARD_ITEMS = [
    {"id": str(uuid.uuid4()), "type": "decision", "title": "Migrate to FalkorDB in production?", "description": "Docker now available on prod servers. FalkorDB is 11.4x faster than Kuzu.", "status": "backlog", "created_by": "varun", "created_at": (datetime.now(timezone.utc) - timedelta(days=2)).isoformat(), "assigned_to": None, "completed_at": None, "linked_decision_id": None, "linked_pr": None, "priority": "high"},
    {"id": str(uuid.uuid4()), "type": "task", "title": "Run METR third-arm study", "description": "Arm A: no AI. Arm B: AI only. Arm C: AI + CaaS. Prove time savings.", "status": "backlog", "created_by": "varun", "created_at": (datetime.now(timezone.utc) - timedelta(days=1)).isoformat(), "assigned_to": None, "completed_at": None, "linked_decision_id": None, "linked_pr": None, "priority": "medium"},
    {"id": str(uuid.uuid4()), "type": "task", "title": "Add Slack connector", "description": "PR #52 in progress. Events API passive capture.", "status": "in_progress", "created_by": "varun", "created_at": (datetime.now(timezone.utc) - timedelta(days=3)).isoformat(), "assigned_to": "claude-code", "completed_at": None, "linked_decision_id": None, "linked_pr": "52", "priority": "high"},
]

def make_compliance_entries():
    entries = []
    now = datetime.now(timezone.utc)
    events = [
        (2, "claude-code", "get_project_context", 12), (18, "claude-code", "query_decisions", 3),
        (55, "system", "pr_injection", 4), (80, "cursor", "check_constraints", 2),
        (128, "claude-code", "query_decisions", 5), (200, "claude-code", "get_project_context", 12),
        (247, "cursor", "query_decisions", 3), (280, "system", "pr_injection", 2),
        (340, "claude-code", "check_constraints", 4), (410, "claude-code", "get_path_context", 2),
        (480, "cursor", "query_decisions", 1), (520, "claude-code", "add_decision", 0),
        (600, "claude-code", "get_project_context", 12), (650, "system", "pr_injection", 3),
        (720, "cursor", "check_constraints", 2), (800, "claude-code", "query_decisions", 4),
        (850, "claude-code", "get_path_context", 3), (920, "system", "pr_injection", 4),
        (980, "claude-code", "query_decisions", 2), (1020, "cursor", "get_project_context", 12),
    ]
    for i, (mins, agent, tool, count) in enumerate(events):
        entries.append({
            "entry_id": str(uuid.uuid4()),
            "timestamp": (now - timedelta(minutes=mins)).isoformat(),
            "event_type": "context_injection",
            "agent": agent, "tool_name": tool,
            "session_id": "session-abc" if "claude" in agent else "session-def",
            "query": f"query-{i}",
            "decisions_surfaced": [f"d{j}" for j in range(count)],
            "decision_count": count,
            "deja_vu_triggered": (i == 1),
        })
    return entries

async def seed_graph():
    print("Seeding knowledge graph (no API calls)...")
    try:
        from smm_sync.context_graph.client import GraphClient
    except ImportError as e:
        print(f"  Import error: {e}")
        return False

    graph_dir = SMM_DIR / "graph"
    api_key = os.environ.get("ANTHROPIC_API_KEY", "sk-ant-demo")

    try:
        client = GraphClient(graph_dir=graph_dir, api_key=api_key)
        g = await client._get_graphiti()

        for i, d in enumerate(DECISIONS):
            ts = datetime.now(timezone.utc) - timedelta(days=d.get("days_ago", i))
            content = (
                f"Decision: {d['title']}\n"
                f"Rationale: {d['rationale']}\n"
                f"Type: {d['type']}\n"
                f"Confidence: {d['confidence']}\n"
                f"Source: {d['source_type']}\n"
                f"Status: {d.get('status','approved')}\n"
                f"Is_constraint: {d.get('is_constraint',False)}\n"
            )
            if d.get("source_pr"):
                content += f"Source PR: #{d['source_pr']}\n"
            try:
                await g.add_episode(
                    name=d["title"][:80],
                    episode_body=content,
                    source_description=d["source_type"],
                    reference_time=ts,
                )
                print(f"  ✓ {d['title'][:55]}")
            except Exception as e:
                print(f"  ⚠ {d['title'][:40]}: {e}")

        print(f"\n✓ Seeded {len(DECISIONS)} decisions into graph")
        return True
    except Exception as e:
        print(f"  Graph error: {e}")
        return False

def seed_files():
    # Compliance log
    lineage_path = SMM_DIR / "compliance_lineage.jsonl"
    entries = make_compliance_entries()
    with open(lineage_path, "w") as f:
        for e in entries:
            f.write(json.dumps(e) + "\n")
    print(f"✓ {len(entries)} compliance entries")

    # Contradictions
    contradictions_path = SMM_DIR / "contradictions.jsonl"
    with open(contradictions_path, "w") as f:
        for c in CONTRADICTIONS:
            f.write(json.dumps(c) + "\n")
    print(f"✓ {len(CONTRADICTIONS)} contradictions")

    # Board
    (SMM_DIR / "board.json").write_text(json.dumps({"items": BOARD_ITEMS}, indent=2))
    print(f"✓ {len(BOARD_ITEMS)} board items")

    # Capture state
    state = {
        "varunajaytawde/smm-sync": {"last_pr_number": 51, "last_commit_sha": "a1b2c3d", "last_issue_number": 12, "last_release_id": None, "last_run": (datetime.now(timezone.utc) - timedelta(minutes=8)).isoformat()},
        "varunajaytawde/axiom-hub": {"last_pr_number": 7, "last_commit_sha": "e4f5g6h", "last_issue_number": 3, "last_release_id": None, "last_run": (datetime.now(timezone.utc) - timedelta(minutes=8)).isoformat()},
    }
    (SMM_DIR / "capture_state.json").write_text(json.dumps(state, indent=2))
    print("✓ Capture state")

    # Pending decisions for feed
    (SMM_DIR / "pending_decisions.json").write_text(json.dumps({"pending": PENDING_DECISIONS}, indent=2))
    print(f"✓ {len(PENDING_DECISIONS)} pending PR decisions for feed")

async def main():
    print("━" * 44)
    print("  CaaS Demo Data Seeder — Zero API Cost")
    print("━" * 44)
    print()
    seed_files()
    print()
    graph_ok = await seed_graph()
    print()
    print("━" * 44)
    if graph_ok:
        print("  ✓ All demo data seeded")
        print("  Run: smm dashboard")
    else:
        print("  ⚠ Files seeded. Graph needs API key.")
        print("  Set ANTHROPIC_API_KEY and re-run.")
    print("━" * 44)

if __name__ == "__main__":
    asyncio.run(main())
