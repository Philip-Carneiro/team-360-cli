"""JIRA data collectors for 360 status reports.

All queries use JIRA API v3 (/rest/api/3/).
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import re
import subprocess
from datetime import datetime, timezone
from typing import Any

import requests

from collectors._dates import _days_since, _parse_iso

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _jira_get(session: requests.Session, base_url: str, path: str, params: dict | None = None) -> Any:
    url = f"{base_url.rstrip('/')}{path}"
    r = session.get(url, params=params)
    r.raise_for_status()
    return r.json()


def _jira_search(
    session: requests.Session, base_url: str, jql: str, fields: str = "", max_results: int = 100, expand: str = ""
) -> list[dict]:
    """Paginated JQL search via v3 endpoint (token-based pagination)."""
    results: list[dict] = []
    next_token: str | None = None
    while True:
        params: dict[str, Any] = {"jql": jql, "maxResults": min(max_results - len(results), 100)}
        if fields:
            params["fields"] = fields
        if expand:
            params["expand"] = expand
        if next_token:
            params["nextPageToken"] = next_token
        data = _jira_get(session, base_url, "/rest/api/3/search/jql", params)
        issues = data.get("issues", [])
        results.extend(issues)
        next_token = data.get("nextPageToken")
        if not next_token or not issues or len(results) >= max_results:
            break
    return results[:max_results]


def _make_session(jira_auth: tuple[str, str]) -> requests.Session:
    s = requests.Session()
    s.auth = jira_auth
    s.headers["Accept"] = "application/json"
    return s


def _base_url(config: dict) -> str:
    return config.get("jira_base_url") or os.environ.get("JIRA_BASE_URL") or os.environ.get("JIRA_URL", "")


def _name_match(name_a: str, name_b: str) -> bool:
    """Case-insensitive substring match for assignee→roster mapping."""
    if not name_a or not name_b:
        return False
    return name_a.lower() in name_b.lower() or name_b.lower() in name_a.lower()


def _classify_assignee(assignee_name: str | None, roster: list) -> str:
    if not assignee_name:
        return "UNASSIGNED"
    for member in roster:
        name = member["name"] if isinstance(member, dict) else str(member)
        if _name_match(assignee_name, name):
            return "ROSTER"
    return "THIRD-PARTY"


def _extract_text_from_adf(node: Any) -> str:
    """Walk ADF (Atlassian Document Format) nodes to extract plain text."""
    if isinstance(node, str):
        return node
    if isinstance(node, list):
        return " ".join(_extract_text_from_adf(n) for n in node)
    if isinstance(node, dict):
        text = node.get("text", "")
        for child in node.get("content", []):
            text += _extract_text_from_adf(child)
        for mark in node.get("marks", []):
            if mark.get("type") == "link":
                href = mark.get("attrs", {}).get("href", "")
                if href:
                    text += " " + href
        return text
    return ""


def _extract_urls_from_pr_field(field_value: Any) -> list[dict]:
    """Extract PR/MR URLs from customfield_10875 (string or ADF dict)."""
    raw_text = ""
    if isinstance(field_value, str):
        raw_text = field_value
    elif isinstance(field_value, dict):
        raw_text = _extract_text_from_adf(field_value)

    if not raw_text:
        return []

    url_pattern = re.compile(r'https?://[^\s<>"\')\]]+(?:/pull/\d+|/merge_requests/\d+)')
    urls: list[dict] = []
    seen: set[str] = set()
    for url in url_pattern.findall(raw_text):
        url = url.strip().rstrip(".,;")
        norm = url.rstrip("/")
        if norm in seen:
            continue
        seen.add(norm)
        if "github.com" in url and "/pull/" in url:
            urls.append({"url": url, "platform": "github"})
        elif "merge_requests" in url:
            urls.append({"url": url, "platform": "gitlab"})
    return urls


def _extract_urls_from_remotelinks(session: requests.Session, base_url: str, key: str) -> list[dict]:
    """Fetch remote links and extract PR/MR URLs."""
    try:
        links = _jira_get(session, base_url, f"/rest/api/3/issue/{key}/remotelink")
    except Exception:
        log.warning("Failed to fetch remote links for %s", key)
        return []
    urls: list[dict] = []
    for link in links:
        href = link.get("object", {}).get("url", "")
        if not href:
            continue
        if "github.com" in href and "/pull/" in href:
            urls.append({"url": href, "platform": "github"})
        elif "merge_requests" in href:
            urls.append({"url": href, "platform": "gitlab"})
    return urls


def _check_pr_status(url: str, platform: str) -> dict:
    """Check actual PR/MR state using gh/glab CLI."""
    result = {"url": url, "platform": platform, "state": "UNKNOWN", "checked": False}
    try:
        if platform == "github":
            proc = subprocess.run(
                ["gh", "pr", "view", url, "--json", "state,mergedAt,url,createdAt"],
                capture_output=True,
                text=True,
                timeout=30,
            )
            if proc.returncode == 0:
                data = json.loads(proc.stdout)
                result["state"] = "MERGED" if data.get("mergedAt") else data.get("state", "OPEN").upper()
                result["checked"] = True
                age = _days_since(_parse_iso(data.get("createdAt")))
                if age is not None:
                    result["age_days"] = age
        elif platform == "gitlab":
            gitlab_host = os.environ.get("GITLAB_HOST", "")
            match = re.search(r"https?://[^/]+/(.+)/-/merge_requests/(\d+)", url)
            if match and gitlab_host:
                project_path = match.group(1).replace("/", "%2F")
                mr_id = match.group(2)
                proc = subprocess.run(
                    ["glab", "api", f"projects/{project_path}/merge_requests/{mr_id}", "--hostname", gitlab_host],
                    capture_output=True,
                    text=True,
                    timeout=30,
                )
                if proc.returncode == 0:
                    data = json.loads(proc.stdout)
                    state = data.get("state", "opened")
                    result["state"] = "MERGED" if state == "merged" else "OPEN" if state == "opened" else state.upper()
                    result["checked"] = True
                    age = _days_since(_parse_iso(data.get("created_at")))
                    if age is not None:
                        result["age_days"] = age
    except Exception as e:
        log.warning("PR status check failed for %s: %s", url, e)
    return result


def _check_pr_statuses(pr_urls: list[dict]) -> list[dict]:
    """Check status of each PR/MR URL. Input: [{'url': ..., 'platform': ...}]."""
    results = []
    for pr in pr_urls:
        checked = _check_pr_status(pr["url"], pr["platform"])
        results.append(checked)
    return results


def _compute_composite_pr_status(pr_links: list[dict]) -> str:
    if not pr_links:
        return "NO_PRS"
    states = {pr["state"] for pr in pr_links}
    if states == {"MERGED"}:
        return "ALL_MERGED"
    if "MERGED" in states and "OPEN" in states:
        return "PARTIALLY_MERGED"
    if states == {"OPEN"}:
        return "ALL_OPEN"
    return "MIXED"


def _get_all_pr_links(session: requests.Session, base_url: str, issue: dict) -> list[dict]:
    """Get all PR/MR URLs from both customfield_10875 and remote links, deduplicated."""
    key = issue["key"]
    field_val = issue.get("fields", {}).get("customfield_10875")
    urls = _extract_urls_from_pr_field(field_val)
    urls.extend(_extract_urls_from_remotelinks(session, base_url, key))

    seen: set[str] = set()
    deduped: list[dict] = []
    for u in urls:
        if u["url"] not in seen:
            seen.add(u["url"])
            deduped.append(u)

    checked: list[dict] = []
    for u in deduped:
        checked.append(_check_pr_status(u["url"], u["platform"]))
    return checked


def _days_in_status(issue: dict) -> int:
    """Dias desde que o ticket entrou no status ATUAL (última transição de status para o status corrente)."""
    fields = issue.get("fields", {})
    current = (fields.get("status", {}) or {}).get("name", "")
    current_l = current.lower()
    today = datetime.now(timezone.utc)
    histories = (issue.get("changelog", {}) or {}).get("histories", [])
    entered: list[datetime] = []
    for h in histories:
        for item in h.get("items", []):
            if item.get("field") == "status" and (item.get("toString") or "").lower() == current_l:
                with contextlib.suppress(ValueError, KeyError):
                    entered.append(datetime.fromisoformat(h["created"].replace("Z", "+00:00")))
    if entered:
        return max((today - max(entered)).days, 0)
    created = fields.get("created")
    if created:
        try:
            return max((today - datetime.fromisoformat(created.replace("Z", "+00:00"))).days, 0)
        except (ValueError, KeyError):
            pass
    return 0


def _ticket_base(issue: dict, roster: list[str]) -> dict:
    """Extract common ticket fields."""
    fields = issue.get("fields", {})
    assignee = fields.get("assignee")
    assignee_name = assignee.get("displayName", "") if assignee else None
    # Extract PR links from customfield_10875 for ALL tickets (not just Review)
    prs = _extract_urls_from_pr_field(fields.get("customfield_10875"))

    parent = fields.get("parent")
    parent_key = parent.get("key", "") if isinstance(parent, dict) else ""

    return {
        "key": issue["key"],
        "summary": fields.get("summary", ""),
        "status": fields.get("status", {}).get("name", ""),
        "priority": fields.get("priority", {}).get("name", ""),
        "issuetype": fields.get("issuetype", {}).get("name", ""),
        "assignee": assignee_name,
        "classification": _classify_assignee(assignee_name, roster),
        "activity_type": (fields.get("customfield_10464") or {}).get("value", "")
        if isinstance(fields.get("customfield_10464"), dict)
        else (fields.get("customfield_10464") or ""),
        "parent_key": parent_key,
        "prs": prs,
    }


# ---------------------------------------------------------------------------
# Query functions
# ---------------------------------------------------------------------------


def collect_doing_board(
    config: dict, jira_auth: tuple, prev_360_date: str, swimlane_jql: str | None = None
) -> list[dict]:
    """Query 1: In Progress, Review, Testing tickets with days_in_status."""
    session = _make_session(jira_auth)
    base = _base_url(config)
    label = config["jira_label"]
    components = config.get("jira_components", "")
    projects = config.get("jira_projects", "")
    roster = config.get("roster", [])

    jql_parts = [
        f'labels = "{label}"',
        'status IN ("In Progress", "Review", "In Review", "Testing")',
    ]
    if components:
        jql_parts.append(f'component = "{components}"')
    if projects:
        jql_parts.append(f"project IN ({projects})")
    if swimlane_jql:
        jql_parts.append(swimlane_jql)
    jql = " AND ".join(jql_parts) + " ORDER BY status ASC, priority DESC"

    fields = "summary,status,priority,assignee,issuetype,customfield_10875,customfield_10464,parent,created"
    issues = _jira_search(session, base, jql, fields=fields)

    results: list[dict] = []
    for issue in issues:
        try:
            detail = _jira_get(session, base, f"/rest/api/3/issue/{issue['key']}", {"expand": "changelog"})
            issue["changelog"] = detail.get("changelog", {})
        except Exception:
            log.warning("Failed to fetch changelog for %s", issue["key"])
            issue["changelog"] = {}

        ticket = _ticket_base(issue, roster)
        ticket["days_in_status"] = _days_in_status(issue)

        # Check PR status for ALL tickets that have PR links
        if ticket["prs"]:
            ticket["pr_links"] = _check_pr_statuses(ticket["prs"])
            ticket["composite_pr_status"] = _compute_composite_pr_status(ticket["pr_links"])
        else:
            # Try remote links as fallback
            try:
                remote_prs = _get_all_pr_links(session, base, issue)
                if remote_prs:
                    ticket["pr_links"] = remote_prs
                    ticket["composite_pr_status"] = _compute_composite_pr_status(remote_prs)
                else:
                    ticket["pr_links"] = []
                    ticket["composite_pr_status"] = "NO_PRS"
            except Exception:
                ticket["pr_links"] = []
                ticket["composite_pr_status"] = "NO_PRS"

        results.append(ticket)

    return results


def collect_backlog(config: dict, jira_auth: tuple) -> list[dict]:
    """Query 2: Top 15 backlog items, flag buried criticals at rank 6+."""
    session = _make_session(jira_auth)
    base = _base_url(config)
    label = config["jira_label"]
    components = config.get("jira_components", "")
    projects = config.get("jira_projects", "")
    roster = config.get("roster", [])

    jql_parts = [
        f'labels = "{label}"',
        'status IN ("Backlog", "New")',
    ]
    if components:
        jql_parts.append(f'component = "{components}"')
    if projects:
        jql_parts.append(f"project IN ({projects})")
    jql = " AND ".join(jql_parts) + " ORDER BY Rank ASC"

    issues = _jira_search(session, base, jql, fields="summary,status,priority,assignee,issuetype", max_results=15)

    results: list[dict] = []
    for i, issue in enumerate(issues, 1):
        ticket = _ticket_base(issue, roster)
        ticket["rank"] = i
        results.append(ticket)

    return results


def collect_bugs(config: dict, jira_auth: tuple) -> list[dict]:
    """Query 3: Open bugs via filter ID."""
    session = _make_session(jira_auth)
    base = _base_url(config)
    roster = config.get("roster", [])
    filter_id = config.get("bugs_filter_id")

    if not filter_id:
        log.warning("No bugs_filter_id in config, skipping bug collection")
        return []

    try:
        filt = _jira_get(session, base, f"/rest/api/3/filter/{filter_id}")
        jql = filt["jql"]
    except Exception:
        log.warning("Failed to fetch filter %s", filter_id)
        return []

    issues = _jira_search(session, base, jql, fields="summary,status,priority,assignee,issuetype")
    return [_ticket_base(issue, roster) for issue in issues]


def collect_strats(config: dict, jira_auth: tuple) -> dict:
    """Query 4: Active Features/STRATs, split committed/planning if STRAT boards exist.

    Always excludes Release Pending.
    """
    session = _make_session(jira_auth)
    base = _base_url(config)
    label = config["jira_label"]
    components = config.get("jira_components", "")
    roster = config.get("roster", [])
    has_strat_boards = bool(config.get("strat_boards"))

    base_jql = f'issuetype = Feature AND labels = "{label}"'
    if components:
        base_jql += f' AND component = "{components}"'
    exclude = " AND status NOT IN (Done, Closed, 'Release Pending')"

    strat_fields = "summary,status,priority,assignee,issuetype,customfield_10712,customfield_10814,fixVersions"

    def _enrich(issues: list[dict]) -> list[dict]:
        results = []
        for issue in issues:
            ticket = _ticket_base(issue, roster)
            fields = issue.get("fields", {})

            color_field = fields.get("customfield_10712")
            ticket["color"] = color_field.get("value", "") if isinstance(color_field, dict) else (color_field or "")

            summary_field = fields.get("customfield_10814")
            ticket["status_summary"] = _extract_text_from_adf(summary_field) if summary_field else ""

            fix_versions = fields.get("fixVersions", [])
            ticket["fix_versions"] = [v.get("name", "") for v in fix_versions] if fix_versions else []

            results.append(ticket)
        return results

    if has_strat_boards:
        committed_jql = base_jql + " AND fixVersion IS NOT EMPTY" + exclude + " ORDER BY status ASC"
        planning_jql = base_jql + " AND fixVersion IS EMPTY" + exclude + " ORDER BY status ASC"

        committed_issues = _jira_search(session, base, committed_jql, fields=strat_fields)
        planning_issues = _jira_search(session, base, planning_jql, fields=strat_fields)

        return {
            "committed": _enrich(committed_issues),
            "planning": _enrich(planning_issues),
        }
    else:
        all_jql = base_jql + exclude + " ORDER BY status ASC"
        all_issues = _jira_search(session, base, all_jql, fields=strat_fields)
        return {
            "committed": _enrich(all_issues),
            "planning": [],
        }


def collect_completed(config: dict, jira_auth: tuple, since_date: str) -> list[dict]:
    """Query 5: Completed since previous 360."""
    session = _make_session(jira_auth)
    base = _base_url(config)
    label = config["jira_label"]
    components = config.get("jira_components", "")
    projects = config.get("jira_projects", "")
    roster = config.get("roster", [])

    jql_parts = [
        f'labels = "{label}"',
        "status IN (Done, Closed)",
        f'status CHANGED TO (Done, Closed) AFTER "{since_date}"',
    ]
    if components:
        jql_parts.append(f'component = "{components}"')
    if projects:
        jql_parts.append(f"project IN ({projects})")
    jql = " AND ".join(jql_parts) + " ORDER BY updated DESC"

    issues = _jira_search(session, base, jql, fields="summary,status,priority,assignee,issuetype")
    return [_ticket_base(issue, roster) for issue in issues]


def collect_testing_transitions(config: dict, jira_auth: tuple, since_date: str) -> list[dict]:
    """Query 7: Tickets that transitioned to Testing since previous 360."""
    session = _make_session(jira_auth)
    base = _base_url(config)
    label = config["jira_label"]
    components = config.get("jira_components", "")
    projects = config.get("jira_projects", "")

    jql_parts = [
        f'labels = "{label}"',
        f"status CHANGED TO 'Testing' AFTER '{since_date}'",
    ]
    if components:
        jql_parts.append(f'component = "{components}"')
    if projects:
        jql_parts.append(f"project IN ({projects})")
    jql = " AND ".join(jql_parts)

    issues = _jira_search(session, base, jql, fields="summary,status,assignee", expand="changelog")

    results: list[dict] = []
    since_dt = (
        datetime.fromisoformat(since_date.replace("Z", "+00:00"))
        if "T" in since_date
        else datetime.strptime(since_date, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    )

    for issue in issues:
        key = issue["key"]
        fields = issue.get("fields", {})
        tested_date = None
        transitioned_by = None

        for h in issue.get("changelog", {}).get("histories", []):
            for item in h.get("items", []):
                if item.get("field") == "status" and (item.get("toString") or "").lower() in ("testing", "in testing"):
                    try:
                        ts = datetime.fromisoformat(h["created"].replace("Z", "+00:00"))
                        if ts >= since_dt and (tested_date is None or ts > tested_date):
                            tested_date = ts
                            transitioned_by = h.get("author", {}).get("displayName")
                    except (ValueError, KeyError):
                        pass

        results.append(
            {
                "key": key,
                "summary": fields.get("summary", ""),
                "tested_date": tested_date.isoformat() if tested_date else None,
                "transitioned_by": transitioned_by,
            }
        )

    return results


def collect_epic_progress(config: dict, jira_auth: tuple, epics: list[dict]) -> list[dict]:
    """Query 8: Epic children with status breakdown and completion %."""
    if not epics:
        return []

    session = _make_session(jira_auth)
    base = _base_url(config)
    roster = config.get("roster", [])

    results: list[dict] = []
    for epic in epics:
        epic_key = epic["key"]

        children_jql = f'"Epic Link" = {epic_key} OR parent = {epic_key}'
        children = _jira_search(
            session, base, children_jql, fields="summary,status,priority,assignee,issuetype", expand="changelog"
        )

        status_groups: dict[str, int] = {"Done": 0, "In Progress": 0, "Review": 0, "Testing": 0, "Backlog": 0}
        child_results: list[dict] = []

        for child in children:
            ticket = _ticket_base(child, roster)
            ticket["days_in_status"] = _days_in_status(child)

            status = ticket["status"]
            if status in ("Done", "Closed"):
                status_groups["Done"] += 1
            elif status in ("In Progress",):
                status_groups["In Progress"] += 1
            elif status in ("Review", "In Review"):
                status_groups["Review"] += 1
            elif status in ("Testing", "In Testing"):
                status_groups["Testing"] += 1
            else:
                status_groups["Backlog"] += 1

            child_results.append(ticket)

        total = len(child_results)
        done = status_groups["Done"]

        results.append(
            {
                "key": epic_key,
                "summary": epic.get("summary", ""),
                "status": epic.get("status", ""),
                "days_in_status": epic.get("days_in_status", 0),
                "total_children": total,
                "done_count": done,
                "in_progress_count": status_groups["In Progress"],
                "review_count": status_groups["Review"],
                "testing_count": status_groups["Testing"],
                "backlog_count": status_groups["Backlog"],
                "children": child_results,
            }
        )

    return results


def collect_learning_tickets(config: dict, jira_auth: tuple) -> list[dict]:
    """Find Learning tickets for the current release cycle.

    Tries sprint-based query first, falls back to active status query for Kanban boards.
    """
    session = _make_session(jira_auth)
    base = _base_url(config)
    label = config["jira_label"]
    components = config.get("jira_components", "")
    roster = config.get("roster", [])

    # Try sprint-based query first
    jql_parts = [
        f'labels = "{label}"',
        'summary ~ "Learning"',
        "sprint in openSprints()",
    ]
    if components:
        jql_parts.append(f'component = "{components}"')

    try:
        issues = _jira_search(session, base, " AND ".join(jql_parts), fields="summary,status,assignee,issuetype")
        if issues:
            return [_ticket_base(issue, roster) for issue in issues]
    except Exception:
        log.debug("Sprint-based learning query failed, trying fallback")

    # Fallback: active learning tickets (no sprint filter — works on Kanban boards)
    jql_parts_fb = [
        f'labels = "{label}"',
        'summary ~ "Learning"',
        "status NOT IN (Done, Closed)",
    ]
    if components:
        jql_parts_fb.append(f'component = "{components}"')

    try:
        issues = _jira_search(session, base, " AND ".join(jql_parts_fb), fields="summary,status,assignee,issuetype")
        return [_ticket_base(issue, roster) for issue in issues]
    except Exception as e:
        log.warning("Learning ticket query failed: %s", e)
        return []


def collect_board_swimlanes(config: dict, jira_auth: tuple, doing_board: list[dict]) -> list[dict]:
    """Discover board swimlanes by querying active tickets and grouping by fixVersion.

    Uses a single JQL query to find all active tickets with the team label,
    then groups them by fixVersion to build the swimlane list.
    """
    session = _make_session(jira_auth)
    base = _base_url(config)
    label = config.get("jira_label", "")
    components = config.get("jira_components", "")

    # Build JQL for active tickets
    jql_parts = [
        f'labels = "{label}"',
        'status IN ("In Progress", "Review", "In Review", "Testing")',
    ]
    if components:
        jql_parts.append(f'component = "{components}"')

    try:
        result = _jira_get(
            session,
            base,
            "/rest/api/3/search/jql",
            {
                "jql": " AND ".join(jql_parts),
                "fields": "fixVersions",
                "maxResults": 200,
            },
        )
    except Exception as e:
        log.warning("Failed to fetch tickets for swimlane discovery: %s", e)
        return []

    issues = result.get("issues", [])
    if not issues:
        return []

    # Group by fixVersion
    version_counts: dict[str, int] = {}
    no_version_count = 0
    for issue in issues:
        fix_versions = issue.get("fields", {}).get("fixVersions", [])
        if not fix_versions:
            no_version_count += 1
        else:
            for fv in fix_versions:
                name = fv.get("name", "")
                if name:
                    version_counts[name] = version_counts.get(name, 0) + 1

    swimlanes: list[dict] = []
    for name, count in sorted(version_counts.items(), key=lambda x: -x[1]):
        swimlanes.append(
            {
                "key": name,
                "name": name,
                "count": count,
                "jql": f'fixVersion = "{name}"',
            }
        )

    if no_version_count > 0:
        swimlanes.append(
            {
                "key": "__NO_VERSION__",
                "name": "No fix version",
                "count": no_version_count,
                "jql": "fixVersion IS EMPTY",
            }
        )

    swimlanes.sort(key=lambda s: s["count"], reverse=True)
    return swimlanes
