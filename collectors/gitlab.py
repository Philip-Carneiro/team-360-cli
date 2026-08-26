"""GitLab MR collector for 360 status reports.

Uses `glab` CLI. Skips entirely if GITLAB_HOST is not set.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess

from collectors._dates import _days_since, _parse_iso

log = logging.getLogger(__name__)


def collect_gitlab_mrs(config: dict) -> dict:
    """Fetch open MRs for team members from GitLab repos.

    Returns {'open_mrs': [...]} or empty if GITLAB_HOST not set.
    """
    gitlab_host = os.environ.get("GITLAB_HOST", "")
    if not gitlab_host:
        log.info("GITLAB_HOST not set, skipping GitLab collection")
        return {"open_mrs": []}

    repos = config.get("gitlab_repos", [])
    if not repos:
        log.info("No GitLab repos in config, skipping")
        return {"open_mrs": []}

    open_mrs: list[dict] = []

    for repo in repos:
        project_path = repo.replace("/", "%2F")
        proc = subprocess.run(
            [
                "glab",
                "api",
                f"projects/{project_path}/merge_requests?state=opened&per_page=100",
                "--hostname",
                gitlab_host,
            ],
            capture_output=True,
            text=True,
            timeout=60,
        )

        if proc.returncode != 0:
            log.warning("Failed to fetch MRs for %s: %s", repo, proc.stderr)
            continue

        try:
            mrs = json.loads(proc.stdout)
        except json.JSONDecodeError:
            log.warning("Failed to parse glab output for %s", repo)
            continue

        for mr in mrs:
            author = mr.get("author", {}).get("username", "")
            created = _parse_iso(mr.get("created_at"))
            updated = _parse_iso(mr.get("updated_at"))

            open_mrs.append(
                {
                    "repo": repo,
                    "iid": mr.get("iid"),
                    "title": mr.get("title", ""),
                    "author": author,
                    "url": mr.get("web_url", ""),
                    "created_at": mr.get("created_at"),
                    "age_days": _days_since(created) if created else 0,
                    "updated_at": mr.get("updated_at"),
                    "days_since_update": _days_since(updated) if updated else 0,
                    "state": mr.get("state", "opened"),
                    "pipeline_status": mr.get("head_pipeline", {}).get("status") if mr.get("head_pipeline") else None,
                }
            )

    return {"open_mrs": open_mrs}
