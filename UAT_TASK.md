# UAT Test Task

## Your task
Implement the `smm setup` command described in our PRDs.

This is a real feature we need to build. Implement it
properly following our architectural guidelines.

## What you need to implement
A new CLI command `smm setup` that onboards a new
repository to CaaS interactively.

## Requirements
- Detects git remote automatically
- Generates .smm/github.yml
- Checks for required API keys
- Runs initial capture
- Generates ONBOARDING.md
- Prints .mcp.json snippet

Do not ask me for clarification. Use your tools to
find all relevant architectural decisions and constraints
before writing any code. Follow our established patterns.
