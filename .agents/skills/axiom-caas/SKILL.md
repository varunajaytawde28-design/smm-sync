---
name: axiom-caas-protocol
description: Mandatory Context-as-a-Service integration. Use immediately upon session initialization and whenever architectural decisions are made.
---

# Axiom Hub Protocol

Before any code changes:
1. Call `get_project_context` to load active decisions and unresolved contradictions
2. If contradictions exist, STOP and notify the user

When making implementation choices:
- Call `add_decision` for any choice where two or more alternatives existed
- Include: title, description, type (architectural/technical/product/constraint), confidence (0.0-1.0)

See AGENTS.md in the project root for full decision capture rules, few-shot examples, and the Nine-Box granularity framework.
