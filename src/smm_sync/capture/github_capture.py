"""GitHub passive capture for CaaS.

Polls GitHub API for new events and writes them to the
Graphiti knowledge graph as decisions.

Usage:
    capture = GitHubCapture(config_path, graph_client)
    await capture.run_once()    # single poll
    await capture.run_forever() # continuous polling

Requires: GITHUB_TOKEN env var (personal access token)
Uses: GitHub REST API v3 via PyGithub
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Optional

import anthropic
import yaml
from github import Auth, Github, GithubException, RateLimitExceededException

import re

from smm_sync.capture.models import (
    CaptureSettings,
    CapturedEvent,
    GithubCaptureConfig,
    RepoConfig,
)
from smm_sync.security import sanitize_content


def load_config(config_path: Path) -> GithubCaptureConfig:
    """Load and validate .smm/github.yml.

    Args:
        config_path: Path to github.yml file.

    Returns:
        Validated GithubCaptureConfig.

    Raises:
        FileNotFoundError: If config_path does not exist.
        ValueError: If config is missing required fields.
    """
    if not config_path.exists():
        raise FileNotFoundError(f"No github.yml found at {config_path}. Run 'smm capture init'.")
    with config_path.open("r", encoding="utf-8") as f:
        raw = yaml.safe_load(f)
    return GithubCaptureConfig.model_validate(raw)


def load_capture_state(state_path: Path) -> dict:
    """Load capture state from .smm/capture_state.json.

    Args:
        state_path: Path to capture_state.json.

    Returns:
        Dict mapping 'owner/name' -> {last_pr_number, last_commit_sha, ...}.
    """
    if not state_path.exists():
        return {}
    with state_path.open("r", encoding="utf-8") as f:
        return json.load(f)


def save_capture_state(state_path: Path, state: dict) -> None:
    """Atomically write capture state to .smm/capture_state.json.

    Uses a temp file + os.rename for crash safety.

    Args:
        state_path: Path to capture_state.json.
        state: State dict to persist.
    """
    state_path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(dir=state_path.parent, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(state, f, indent=2, default=str)
        os.rename(tmp_path, state_path)
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def keyword_filter(content: str, keywords: list[str]) -> bool:
    """Return True if content contains any decision keyword.

    Args:
        content: Text to check.
        keywords: List of keyword strings to search for (case-insensitive).

    Returns:
        True if at least one keyword is found.
    """
    lower = content.lower()
    return any(kw.lower() in lower for kw in keywords)


async def extract_decision(content: str, source: str, api_key: str) -> Optional[str]:
    """Extract a key decision from content using Claude Haiku.

    Uses claude-haiku-4-5-20251001 for cost efficiency (~$0.001/call).
    Only call this AFTER keyword_filter returns True.

    Args:
        content: Text to extract a decision from.
        source: Human-readable source label (e.g., 'PR #42 title').
        api_key: Anthropic API key.

    Returns:
        One-sentence decision string, or None if no clear decision found.
    """
    client = anthropic.AsyncAnthropic(api_key=api_key)
    prompt = (
        "Extract the key technical or product decision from this text in one sentence. "
        "If there is no clear decision, respond with exactly 'NO_DECISION'.\n\n"
        f"Text: {content[:2000]}"
    )
    try:
        message = await client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=150,
            messages=[{"role": "user", "content": prompt}],
        )
        result = message.content[0].text.strip()
        if result == "NO_DECISION":
            return None
        return result
    except Exception as e:
        print(f"  [warn] extract_decision failed for {source}: {e}", file=sys.stderr)
        return None


class GitHubCapture:
    """Polls GitHub repos and writes decisions to the knowledge graph.

    Args:
        config_path: Path to .smm/github.yml.
        state_path: Path to .smm/capture_state.json.
        graph_client: GraphClient instance for writing decisions.
        github_token: GitHub personal access token (falls back to GITHUB_TOKEN env var).
        api_key: Anthropic API key (falls back to ANTHROPIC_API_KEY env var).
    """

    def __init__(
        self,
        config_path: Path,
        state_path: Path,
        graph_client,
        github_token: Optional[str] = None,
        api_key: Optional[str] = None,
    ) -> None:
        self.config = load_config(config_path)
        self.state_path = state_path
        self.graph_client = graph_client
        self.github_token = github_token or os.environ.get("GITHUB_TOKEN", "")
        self.api_key = api_key or os.environ.get("ANTHROPIC_API_KEY", "")
        self._gh: Optional[Github] = None
        self._anthropic_client: Optional[anthropic.AsyncAnthropic] = None

    def _get_github(self) -> Github:
        """Return (and lazily initialise) the PyGithub client.

        Returns:
            Authenticated Github instance.
        """
        if self._gh is None:
            self._gh = Github(auth=Auth.Token(self.github_token))
        return self._gh

    def _settings(self) -> CaptureSettings:
        """Return capture settings from config.

        Returns:
            CaptureSettings instance.
        """
        return self.config.settings

    def _lookback_since(self) -> datetime:
        """Return the datetime threshold for first-run lookback.

        Returns:
            UTC datetime N days ago (where N = lookback_days).
        """
        return datetime.now(timezone.utc) - timedelta(days=self._settings().lookback_days)

    def _log(self, msg: str) -> None:
        """Print a timestamped log line to stderr.

        Args:
            msg: Message to print.
        """
        ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        print(f"[{ts}] {msg}", file=sys.stderr)

    def _sanitize_content(self, content: str) -> tuple[str, bool]:
        """Scan content for prompt injection patterns before storing in the knowledge graph.

        Args:
            content: Raw content string to sanitize.

        Returns:
            (sanitized_content, was_flagged)

        If flagged:
        - Logs warning to stderr with content hash (never logs the actual content)
        - Replaces suspicious line with [CONTENT FILTERED]
        - Still processes remaining clean content
        - Never raises — silent sanitization only
        """
        return sanitize_content(content)

    async def _call_llm(self, prompt: str, model: str, max_tokens: int) -> str:
        """Call Anthropic API and return the text response.

        Reuses a cached AsyncAnthropic client per GitHubCapture instance.

        Args:
            prompt: User prompt to send to the model.
            model: Anthropic model ID.
            max_tokens: Maximum tokens to generate.

        Returns:
            Stripped text content of the first content block.

        Raises:
            RuntimeError: If the API call fails.
        """
        if self._anthropic_client is None:
            self._anthropic_client = anthropic.AsyncAnthropic(api_key=self.api_key)
        message = await self._anthropic_client.messages.create(
            model=model,
            max_tokens=max_tokens,
            messages=[{"role": "user", "content": prompt}],
        )
        return message.content[0].text.strip()

    async def _verify_github_auth(self) -> bool:
        """Verify GitHub authentication is still valid.

        Called at the start of every capture run.

        Returns:
            True if auth is valid, False if not.
            Never raises.
        """
        try:
            gh = self._get_github()
            user = gh.get_user()
            _ = user.login  # forces API call
            return True
        except Exception as e:
            error_msg = str(e).lower()
            if "401" in error_msg or "bad credentials" in error_msg:
                print(
                    "\n\u274c GitHub authentication failed.\n"
                    "   Your GITHUB_TOKEN may have expired "
                    "or been revoked.\n"
                    "   Run: export GITHUB_TOKEN=ghp_...\n"
                    "   Get a new token: "
                    "https://github.com/settings/tokens\n",
                    file=sys.stderr
                )
            else:
                print(
                    f"\n\u274c GitHub connection error: {e}\n",
                    file=sys.stderr
                )
            return False

    async def extract_decision_two_stage(
        self,
        content: str,
        source: str,
        title: str = "",
    ) -> dict | None:
        """Two-stage DRMiner-style extraction pipeline.

        Stage 1 (Haiku — binary classifier):
        Fast, cheap. Sole job: "does this contain an engineering
        decision?" Returns True/False only. No extraction yet.

        Stage 2 (Sonnet — structured extractor):
        Only called if Stage 1 returns True. Extracts the full
        decision triad into a rigid JSON schema.

        Research basis: DRMiner (ICSE 2024) proved decomposed
        hybrid pipelines achieve F1=0.65 vs F1=0.58 for
        single-shot LLM extraction. 14x improvement in downstream
        code repair tasks when proper rationale is extracted.

        Args:
            content: Text to extract a decision from.
            source: Human-readable source label (e.g., 'PR #42').
            title: Optional title of the PR/commit/issue.

        Returns:
            {
                "chosen_decision": str,
                "rejected_alternatives": list[str],
                "contextual_arguments": str,
                "confidence": float
            }
            or None if Stage 1 returns False or Stage 2 confidence < 0.50.
        """
        # STAGE 1: Binary classifier (Haiku)
        stage1_prompt = f"""You are a binary classifier for software engineering decisions.

Read this text and respond with ONLY "YES" or "NO".

Does this text contain a systemic software engineering decision?
A decision is: an architectural choice, a technical constraint,
a rejected alternative, or a rationale for why something was
built a specific way.

NOT a decision: bug fixes, typos, formatting changes,
dependency version bumps without explanation.

Text: {content[:1000]}

Respond with ONLY: YES or NO"""

        try:
            stage1_response = await self._call_llm(
                prompt=stage1_prompt,
                model="claude-haiku-4-5-20251001",
                max_tokens=5,
            )
        except Exception as e:
            self._log(f"  [warn] Stage 1 failed for {source}: {e}")
            return None

        if "YES" not in stage1_response.upper():
            return None

        # STAGE 2: Structured extraction (Sonnet)
        stage2_prompt = f"""You are an expert software architect extracting
decision records from engineering communication.

Extract the engineering decision from this text into the exact
JSON schema below. If a field cannot be determined, use null.
Respond with ONLY the JSON object, no other text.

Text: {content[:2000]}
Source: {source}
Title: {title}

Required JSON schema:
{{
    "chosen_decision": "What was decided in one clear sentence",
    "rejected_alternatives": ["Alternative 1 that was rejected", "Alternative 2"],
    "contextual_arguments": "Why this decision was made — rationale and constraints",
    "confidence": 0.85
}}

Confidence scoring:
- 0.90-1.0: Explicit decision with clear rationale and alternatives
- 0.70-0.89: Clear decision but limited rationale
- 0.50-0.69: Implicit decision inferred from context
- Below 0.50: Uncertain — return null instead"""

        try:
            stage2_response = await self._call_llm(
                prompt=stage2_prompt,
                model="claude-sonnet-4-6",
                max_tokens=500,
            )
        except Exception as e:
            self._log(f"  [warn] Stage 2 failed for {source}: {e}")
            return None

        try:
            result = json.loads(stage2_response.strip())
            if result.get("confidence", 0) < 0.50:
                return None
            return result
        except Exception:
            return None

    async def inject_pr_context(
        self,
        repo_config: RepoConfig,
        pr_number: int,
        pr_title: str,
        pr_body: str,
        changed_files: list[str],
    ) -> bool:
        """Inject relevant context as a PR comment at PR creation.

        This is the "workflow boundary" event identified by ProAIDE
        (JetBrains 2026) as achieving 52% engagement vs 62% dismissal
        for mid-task interruptions. PR creation is the optimal
        cognitive load state for receiving architectural context.

        The METR RCT showed developers are 19% slower with AI because
        AI violates implicit constraints. This method is the direct
        antidote — injecting constraints BEFORE code generation begins.

        Algorithm:
        1. Extract key terms from PR title + body + changed files
        2. Query knowledge graph for relevant constraints
        3. Post a structured comment if constraints found
        4. Skip if no relevant constraints found (no noise)

        Args:
            repo_config: Config for the repo (owner, name, project).
            pr_number: PR number to comment on.
            pr_title: Title of the PR.
            pr_body: Body/description of the PR.
            changed_files: List of file paths changed in the PR.

        Returns:
            True if comment posted, False if skipped or failed.
        """
        query = f"{pr_title} {pr_body[:500]} {' '.join(changed_files[:10])}"

        try:
            results = await self.graph_client.search_context(
                query=query,
                project=repo_config.project,
                limit=3,
            )
        except Exception as e:
            self._log(f"  [warn] Context search failed for PR #{pr_number}: {e}")
            return False

        if not results:
            return False

        comment_lines = [
            "## 🧠 CaaS Context",
            "",
            "Relevant architectural decisions for this PR:",
            "",
        ]

        for i, result in enumerate(results, 1):
            comment_lines.append(f"**{i}. {result.title}**")
            comment_lines.append(f"> {result.excerpt}")
            comment_lines.append("")

        comment_lines.extend([
            "---",
            "_Generated by CaaS — Context-as-a-Service_",
            "_[View full decision history](smm decisions)_",
        ])

        comment = "\n".join(comment_lines)

        try:
            gh = self._get_github()
            repo = gh.get_repo(f"{repo_config.owner}/{repo_config.name}")
            pr = repo.get_pull(pr_number)
            pr.create_issue_comment(comment)
            return True
        except Exception as e:
            self._log(f"  [warn] Could not post PR comment: {e}")
            return False

    async def capture_pull_requests(self, repo_config: RepoConfig, state: dict, since_date=None) -> int:
        """Fetch new PRs since last run and write decisions to the graph.

        Uses the two-stage DRMiner pipeline (Stage 1: Haiku classifier,
        Stage 2: Sonnet structured extractor). Also injects CaaS context
        as a PR comment when pr_context_injection is enabled.

        Args:
            repo_config: Config for this repo.
            state: Current capture state dict (mutated in-place).
            since_date: Optional datetime. If provided, backfill from this date
                        ignoring last_pr_number in capture state.

        Returns:
            Count of PRs captured.
        """
        gh = self._get_github()
        settings = self._settings()
        full_name = repo_config.full_name
        repo_state = state.setdefault(full_name, {})
        if since_date:
            # Backfill mode — ignore last seen state
            last_pr = 0
            since = since_date
        else:
            last_pr = repo_state.get("last_pr_number", 0)
            since = self._lookback_since() if last_pr == 0 else None

        try:
            repo = gh.get_repo(full_name)
        except GithubException as e:
            self._log(f"  [error] Cannot access {full_name}: {e}")
            return 0

        count = 0
        new_last_pr = last_pr
        prs = repo.get_pulls(state="closed", sort="updated", direction="desc")

        for pr in prs:
            if pr.number <= last_pr:
                break
            if since and pr.updated_at.replace(tzinfo=timezone.utc) < since:
                break
            body = pr.body or ""
            combined = f"{pr.title}\n\n{body}"
            if len(combined) < settings.min_content_length:
                continue
            if not keyword_filter(combined, settings.decision_keywords):
                continue

            combined, was_flagged = self._sanitize_content(combined)
            if was_flagged:
                print(
                    f"  \u26a0\ufe0f  Sanitized potentially malicious content "
                    f"in PR #{pr.number}",
                    file=sys.stderr
                )
            self._log(f"  PR #{pr.number}: \"{pr.title}\" \u2192 two-stage extraction...")
            result = await self.extract_decision_two_stage(
                combined, f"PR #{pr.number}", pr.title
            )

            # Inject context comment for new PRs (workflow boundary injection)
            if settings.pr_context_injection:
                try:
                    changed_files = [f.filename for f in pr.get_files()]
                except Exception:
                    changed_files = []
                await self.inject_pr_context(
                    repo_config=repo_config,
                    pr_number=pr.number,
                    pr_title=pr.title,
                    pr_body=body,
                    changed_files=changed_files,
                )

            event = CapturedEvent(
                repo=full_name,
                event_type="pr",
                event_id=str(pr.number),
                title=pr.title,
                content=combined[:3000],
                url=pr.html_url,
                decision_extracted=result.get("chosen_decision") if result else None,
            )
            if result:
                decision_content = result.get("chosen_decision", "")
                alternatives = result.get("rejected_alternatives") or []
                rationale = result.get("contextual_arguments") or body[:500] or pr.title
                await self.graph_client.add_decision(
                    title=f"[{full_name}] PR #{pr.number}: {pr.title[:60]}",
                    content=decision_content,
                    rationale=rationale,
                    made_by=f"github:{pr.user.login if pr.user else 'unknown'}",
                    project=repo_config.project,
                    alternatives=alternatives,
                    decision_type="technical",
                )
                self._log(f"    → decision extracted and written to graph")
            else:
                self._log(f"    → no clear decision found, skipping")
                continue

            new_last_pr = max(new_last_pr, pr.number)
            count += 1

        if new_last_pr > last_pr:
            repo_state["last_pr_number"] = new_last_pr
        repo_state["last_run"] = datetime.now(timezone.utc).isoformat()
        return count

    async def capture_commits(self, repo_config: RepoConfig, state: dict, since_date=None) -> int:
        """Fetch new commits since last run and write decisions to the graph.

        Uses the two-stage DRMiner pipeline for extraction.

        Args:
            repo_config: Config for this repo.
            state: Current capture state dict (mutated in-place).
            since_date: Optional datetime. If provided, backfill from this date
                        ignoring last_commit_sha in capture state.

        Returns:
            Count of commits captured.
        """
        gh = self._get_github()
        settings = self._settings()
        full_name = repo_config.full_name
        repo_state = state.setdefault(full_name, {})
        if since_date:
            # Backfill mode — ignore last seen state
            last_sha = None
            since = since_date
        else:
            last_sha = repo_state.get("last_commit_sha")
            since = self._lookback_since() if not last_sha else None

        try:
            repo = gh.get_repo(full_name)
        except GithubException as e:
            self._log(f"  [error] Cannot access {full_name}: {e}")
            return 0

        count = 0
        new_last_sha = last_sha
        kwargs: dict = {}
        if since:
            kwargs["since"] = since

        commits = repo.get_commits(**kwargs)

        for commit in commits:
            sha = commit.sha
            if sha == last_sha:
                break
            if new_last_sha is None:
                new_last_sha = sha  # first one seen = most recent
            msg = commit.commit.message or ""
            if msg.startswith("Merge pull request #"):
                continue
            if len(msg) < settings.min_content_length:
                continue
            if not keyword_filter(msg, settings.decision_keywords):
                continue

            msg, was_flagged = self._sanitize_content(msg)
            if was_flagged:
                print(
                    f"  \u26a0\ufe0f  Sanitized potentially malicious content "
                    f"in commit {sha[:8]}",
                    file=sys.stderr
                )
            self._log(f"  commit {sha[:8]}: \"{msg[:60]}\" \u2192 two-stage extraction...")
            result = await self.extract_decision_two_stage(
                msg, f"commit {sha[:8]}", msg.split("\n")[0][:80]
            )

            if result:
                decision_content = result.get("chosen_decision", "")
                alternatives = result.get("rejected_alternatives") or []
                rationale = result.get("contextual_arguments") or msg[:500]
                await self.graph_client.add_decision(
                    title=f"[{full_name}] commit {sha[:8]}: {msg[:60]}",
                    content=decision_content,
                    rationale=rationale,
                    made_by=f"github:{commit.author.login if commit.author else 'unknown'}",
                    project=repo_config.project,
                    alternatives=alternatives,
                    decision_type="technical",
                )
                self._log(f"    → decision extracted and written to graph")
                count += 1
            else:
                self._log(f"    → no clear decision found, skipping")

        if new_last_sha and new_last_sha != last_sha:
            repo_state["last_commit_sha"] = new_last_sha
        repo_state["last_run"] = datetime.now(timezone.utc).isoformat()
        return count

    async def capture_issues(self, repo_config: RepoConfig, state: dict, since_date=None) -> int:
        """Fetch new/updated issues and write decisions to the graph.

        Uses the two-stage DRMiner pipeline for extraction.

        Args:
            repo_config: Config for this repo.
            state: Current capture state dict (mutated in-place).
            since_date: Optional datetime. If provided, backfill from this date
                        ignoring last_issue_number in capture state.

        Returns:
            Count of issues captured.
        """
        gh = self._get_github()
        settings = self._settings()
        full_name = repo_config.full_name
        repo_state = state.setdefault(full_name, {})
        if since_date:
            # Backfill mode — ignore last seen state
            last_issue = 0
            since = since_date
        else:
            last_issue = repo_state.get("last_issue_number", 0)
            since = self._lookback_since() if last_issue == 0 else None

        try:
            repo = gh.get_repo(full_name)
        except GithubException as e:
            self._log(f"  [error] Cannot access {full_name}: {e}")
            return 0

        count = 0
        new_last_issue = last_issue
        kwargs: dict = {"state": "all", "sort": "updated", "direction": "desc"}
        if since:
            kwargs["since"] = since

        issues = repo.get_issues(**kwargs)

        for issue in issues:
            if issue.number <= last_issue:
                break
            if issue.pull_request:
                continue  # skip PRs surfaced in issues endpoint
            body = issue.body or ""
            combined = f"{issue.title}\n\n{body}"
            if len(combined) < settings.min_content_length:
                continue
            if not keyword_filter(combined, settings.decision_keywords):
                continue

            combined, was_flagged = self._sanitize_content(combined)
            if was_flagged:
                print(
                    f"  \u26a0\ufe0f  Sanitized potentially malicious content "
                    f"in issue #{issue.number}",
                    file=sys.stderr
                )
            self._log(f"  issue #{issue.number}: \"{issue.title}\" \u2192 two-stage extraction...")
            result = await self.extract_decision_two_stage(
                combined, f"issue #{issue.number}", issue.title
            )

            if result:
                decision_content = result.get("chosen_decision", "")
                alternatives = result.get("rejected_alternatives") or []
                rationale = result.get("contextual_arguments") or body[:500] or issue.title
                await self.graph_client.add_decision(
                    title=f"[{full_name}] issue #{issue.number}: {issue.title[:60]}",
                    content=decision_content,
                    rationale=rationale,
                    made_by=f"github:{issue.user.login if issue.user else 'unknown'}",
                    project=repo_config.project,
                    alternatives=alternatives,
                    decision_type="technical",
                )
                self._log(f"    → decision extracted and written to graph")
                new_last_issue = max(new_last_issue, issue.number)
                count += 1
            else:
                self._log(f"    → no clear decision found, skipping")

        if new_last_issue > last_issue:
            repo_state["last_issue_number"] = new_last_issue
        repo_state["last_run"] = datetime.now(timezone.utc).isoformat()
        return count

    async def capture_releases(self, repo_config: RepoConfig, state: dict, since_date=None) -> int:
        """Fetch new releases and write their notes to the graph.

        All release notes are worth capturing without keyword filtering.
        Uses two-stage extraction for structured decision records.

        Args:
            repo_config: Config for this repo.
            state: Current capture state dict (mutated in-place).
            since_date: Optional datetime. If provided, backfill mode (ignore last_release_id).

        Returns:
            Count of releases captured.
        """
        gh = self._get_github()
        full_name = repo_config.full_name
        repo_state = state.setdefault(full_name, {})
        last_release_id = None if since_date else repo_state.get("last_release_id")

        try:
            repo = gh.get_repo(full_name)
        except GithubException as e:
            self._log(f"  [error] Cannot access {full_name}: {e}")
            return 0

        count = 0
        new_last_id = last_release_id
        releases = repo.get_releases()

        for release in releases:
            if last_release_id and release.id <= last_release_id:
                break
            if new_last_id is None:
                new_last_id = release.id
            body = release.body or ""
            combined = f"{release.tag_name}: {release.name or ''}\n\n{body}"

            combined, was_flagged = self._sanitize_content(combined)
            if was_flagged:
                print(
                    f"  \u26a0\ufe0f  Sanitized potentially malicious content "
                    f"in release {release.tag_name}",
                    file=sys.stderr
                )
            self._log(f"  release {release.tag_name}: \"{release.name}\" \u2192 two-stage extraction...")
            result = await self.extract_decision_two_stage(
                combined, f"release {release.tag_name}", release.name or release.tag_name
            )

            decision_content = (
                result.get("chosen_decision") if result else combined[:500]
            )
            alternatives = (result.get("rejected_alternatives") or []) if result else []
            rationale = (
                (result.get("contextual_arguments") or body[:500])
                if result
                else ("Release notes" if not body else body[:500])
            )

            await self.graph_client.add_decision(
                title=f"[{full_name}] release {release.tag_name}: {(release.name or '')[:60]}",
                content=decision_content or combined[:500],
                rationale=rationale,
                made_by=f"github:{release.author.login if release.author else 'unknown'}",
                project=repo_config.project,
                alternatives=alternatives,
                decision_type="process",
            )
            self._log(f"    → release captured and written to graph")
            new_last_id = max(new_last_id or 0, release.id)
            count += 1

        if new_last_id and new_last_id != last_release_id:
            repo_state["last_release_id"] = new_last_id
        repo_state["last_run"] = datetime.now(timezone.utc).isoformat()
        return count

    async def run_once(self, since_date=None):
        """Run a single capture pass across all configured repos.

        Batch approach: Stage 1 (Haiku binary classifier) filters cheap,
        then Stage 2 (Sonnet) only runs for events that passed Stage 1.
        This is handled within extract_decision_two_stage per event.

        Args:
            since_date: Optional datetime. If provided, backfill from this date
                        ignoring per-repo capture state.

        Returns:
            Tuple of (total_captured, total_errors), or dict on auth failure:
            {"error": str, "decisions_captured": int, "auth_valid": bool}
        """
        # Auth check first — fail loudly not silently (Fix 3)
        if not await self._verify_github_auth():
            return {
                "error": "GitHub authentication failed",
                "decisions_captured": 0,
                "auth_valid": False,
            }

        self._log("Starting capture run...")
        if since_date:
            self._log(f"  \U0001f4c5 Backfilling from {since_date.date()}...")
        state = load_capture_state(self.state_path)
        total_captured = 0
        total_errors = 0

        for repo_config in self.config.repos:
            full_name = repo_config.full_name
            self._log(f"{full_name}: starting capture...")

            try:
                if repo_config.capture.pull_requests:
                    self._log(f"{full_name}: checking PRs...")
                    n = await self._safe_capture(
                        self.capture_pull_requests, repo_config, state, since_date
                    )
                    self._log(f"{full_name}: {n} PRs captured")
                    total_captured += n

                if repo_config.capture.commits:
                    self._log(f"{full_name}: checking commits...")
                    n = await self._safe_capture(
                        self.capture_commits, repo_config, state, since_date
                    )
                    self._log(f"{full_name}: {n} commits captured")
                    total_captured += n

                if repo_config.capture.issues:
                    self._log(f"{full_name}: checking issues...")
                    n = await self._safe_capture(
                        self.capture_issues, repo_config, state, since_date
                    )
                    self._log(f"{full_name}: {n} issues captured")
                    total_captured += n

                if repo_config.capture.releases:
                    self._log(f"{full_name}: checking releases...")
                    n = await self._safe_capture(
                        self.capture_releases, repo_config, state, since_date
                    )
                    self._log(f"{full_name}: {n} releases captured")
                    total_captured += n

            except RateLimitExceededException:
                self._log(f"  [warn] Rate limited on {full_name}. Waiting 60s...")
                await asyncio.sleep(60)
                total_errors += 1
            except Exception as e:
                self._log(f"  [error] {full_name}: {e}")
                total_errors += 1

        save_capture_state(self.state_path, state)
        self._log(f"Run complete. {total_captured} events captured, {total_errors} errors.")
        return total_captured, total_errors

    async def _safe_capture(self, method, repo_config: RepoConfig, state: dict, since_date=None) -> int:
        """Call a capture method, returning 0 on any exception.

        Args:
            method: Async capture method to call.
            repo_config: Repo config to pass.
            state: State dict to pass.
            since_date: Optional datetime for backfill mode.

        Returns:
            Count captured, or 0 on error.
        """
        try:
            return await method(repo_config, state, since_date)
        except RateLimitExceededException:
            raise
        except Exception as e:
            self._log(f"  [error] {method.__name__} failed: {e}")
            return 0

    async def run_forever(self) -> None:
        """Poll continuously, sleeping poll_interval_minutes between runs.

        Returns:
            Never returns (runs until process is killed).
        """
        interval = self.config.settings.poll_interval_minutes * 60
        while True:
            await self.run_once()
            self._log(f"Sleeping {self.config.settings.poll_interval_minutes}m until next run...")
            await asyncio.sleep(interval)
