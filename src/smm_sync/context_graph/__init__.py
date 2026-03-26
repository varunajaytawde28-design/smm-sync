"""Context graph module — Graphiti + Kuzu knowledge graph for team decisions.

Exports:
    GraphClient: Thin wrapper around Graphiti with Kuzu backend.
    seed_test_data: Seed the graph with 8 realistic smm-sync decisions.
"""
from smm_sync.context_graph.client import GraphClient
from smm_sync.context_graph.seed import seed_test_data

__all__ = ["GraphClient", "seed_test_data"]
