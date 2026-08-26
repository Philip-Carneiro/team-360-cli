"""GitHub PR collector for 360 status reports.

Uses `gh` CLI for all GitHub API calls.
"""

from __future__ import annotations

import json
import logging
import subprocess
from datetime import datetime, timezone

from collectors._dates import _days_since, _parse_iso

log = logging.getLogger(__name__)

KNOWN_BOTS = {
    "dependabot",
    "renovate",
    "openshift-merge-bot",
    "openshift-ci",
    "red-hat-konflux",
    "konflux-internal-p",
    "openshift-merge-robot",
}

PR_FIELDS = (
    "number,title,author,createdAt,updatedAt,reviewDecision,statusCheckRollup,url,commits,reviews,comments,isDraft"
)


def _name_match(name: str, roster: list[str]) -> bool:
    if not name:
        return False
    name_lower = name.lower()
    return any(name_lower in m.lower() or m.lower() in name_lower for m in roster)


def _compute_days_since_owner_update(pr: dict) -> int:
    """MAX of last commit, last author comment, last author review — never createdAt."""
    author_login = pr.get("author", {}).get("login", "")
    dates: list[datetime] = []

    commits = pr.get("commits", [])
    if commits:
        last_commit_date = _parse_iso(commits[-1].get("committedDate"))
        if last_commit_date:
            dates.append(last_commit_date)

    for comment in pr.get("comments", []):
        if comment.get("author", {}).get("login") == author_login:
            d = _parse_iso(comment.get("createdAt"))
            if d:
                dates.append(d)

    for review in pr.get("reviews", []):
        if review.get("author", {}).get("login") == author_login:
            d = _parse_iso(review.get("submittedAt"))
            if d:
                dates.append(d)

    if not dates:
        fallback = _parse_iso(pr.get("updatedAt"))
        if fallback:
            dates.append(fallback)

    if not dates:
        return 0

    latest = max(dates)
    return max((datetime.now(timezone.utc) - latest).days, 0)


def _compute_pr_health(pr: dict, days_since_owner: int) -> dict:
    """Classify PR into exactly one health category (first match wins)."""
    checks = pr.get("statusCheckRollup", []) or []
    reviews = pr.get("reviews", []) or []
    commits = pr.get("commits", []) or []

    failing_checks = [c.get("name", "unknown") for c in checks if c.get("conclusion") == "FAILURE"]
    if failing_checks:
        return {
            "category": "BUILD_FAILING",
            "detail": f"Build failing: {', '.join(failing_checks)}",
        }

    cr_reviews = [r for r in reviews if r.get("state") == "CHANGES_REQUESTED"]
    if cr_reviews:
        latest_cr = max(cr_reviews, key=lambda r: r.get("submittedAt", ""))
        cr_date = _parse_iso(latest_cr.get("submittedAt"))
        reviewer = latest_cr.get("author", {}).get("login", "unknown")

        last_commit_date = _parse_iso(commits[-1].get("committedDate")) if commits else None

        if cr_date and last_commit_date and last_commit_date > cr_date:
            has_approval_after = any(
                r.get("state") == "APPROVED"
                and _parse_iso(r.get("submittedAt", ""))
                and _parse_iso(r.get("submittedAt", "")) > last_commit_date  # type: ignore[operator]
                for r in reviews
            )
            if not has_approval_after:
                days_ago = _days_since(last_commit_date) or 0
                return {
                    "category": "CHANGES_ADDRESSED_WAITING_REREVIEW",
                    "detail": f"Changes addressed {days_ago}d ago, awaiting re-review from {reviewer}",
                }

        cr_days = _days_since(cr_date) or 0
        return {
            "category": "CHANGES_REQUESTED_NOT_ADDRESSED",
            "detail": f"Changes requested {cr_days}d ago by {reviewer}, not yet addressed",
        }

    if pr.get("reviewDecision") == "APPROVED":
        return {"category": "APPROVED", "detail": "Approved, ready to merge"}

    has_any_review = any(r.get("state") in ("APPROVED", "CHANGES_REQUESTED") for r in reviews)
    if not has_any_review and days_since_owner >= 4:
        return {
            "category": "AWAITING_INITIAL_REVIEW",
            "detail": f"Awaiting initial review ({days_since_owner}d since last owner update)",
        }

    return {
        "category": "ACTIVE",
        "detail": f"Active — last update {days_since_owner}d ago",
    }


def collect_github_prs(config: dict) -> dict:
    """Fetch open PRs for team repos. Returns roster/external/bot split."""
    repos = config.get("github_repos", [])
    roster = config.get("roster", [])
    label = config.get("jira_label", "")

    roster_prs: list[dict] = []
    external_prs: list[dict] = []
    bot_prs: list[dict] = []

    for repo in repos:
        cmd = ["gh", "pr", "list", "--repo", repo, "--state", "open", "--json", PR_FIELDS, "--limit", "100"]
        if label:
            cmd.extend(["--label", label])

        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=60)

        prs: list[dict] = []
        if proc.returncode == 0 and proc.stdout.strip():
            try:
                prs = json.loads(proc.stdout)
            except json.JSONDecodeError:
                log.warning("Failed to parse gh output for %s", repo)

        if not prs and label:
            cmd_no_label = [
                "gh",
                "pr",
                "list",
                "--repo",
                repo,
                "--state",
                "open",
                "--json",
                PR_FIELDS,
                "--limit",
                "100",
            ]
            proc2 = subprocess.run(cmd_no_label, capture_output=True, text=True, timeout=60)
            if proc2.returncode == 0 and proc2.stdout.strip():
                try:
                    prs = json.loads(proc2.stdout)
                except json.JSONDecodeError:
                    log.warning("Failed to parse gh output (no label) for %s", repo)

        for pr in prs:
            if pr.get("isDraft"):
                continue
            author_login = pr.get("author", {}).get("login", "")
            days_since_owner = _compute_days_since_owner_update(pr)
            health = _compute_pr_health(pr, days_since_owner)
            created = _parse_iso(pr.get("createdAt"))

            entry = {
                "repo": repo,
                "number": pr.get("number"),
                "title": pr.get("title", ""),
                "author": author_login,
                "url": pr.get("url", ""),
                "created_at": pr.get("createdAt"),
                "age_days": _days_since(created) if created else 0,
                "days_since_owner_update": days_since_owner,
                "review_decision": pr.get("reviewDecision"),
                "pr_health": health,
                "failing_checks": [
                    c.get("name") for c in (pr.get("statusCheckRollup") or []) if c.get("conclusion") == "FAILURE"
                ],
            }

            if author_login.lower() in KNOWN_BOTS:
                bot_prs.append(entry)
            elif _name_match(author_login, roster):
                roster_prs.append(entry)
            else:
                external_prs.append(entry)

    return {
        "roster_prs": roster_prs,
        "external_prs": external_prs,
        "bot_prs": bot_prs,
    }
