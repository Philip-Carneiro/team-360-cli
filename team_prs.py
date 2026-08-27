#!/usr/bin/env python3
"""Team PRs Tracker — generates a report of open PRs grouped by roster member."""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path

log = logging.getLogger("team-prs")

_SCRIPT_DIR = Path(__file__).resolve().parent


def _check_setup() -> bool:
    required = ["JIRA_BASE_URL", "JIRA_EMAIL", "JIRA_API_TOKEN"]
    missing = [v for v in required if not os.environ.get(v)]
    teams_file = _SCRIPT_DIR / "teams.json"

    if missing:
        print(f"Missing env vars: {', '.join(missing)}")
        return False
    if not teams_file.exists():
        print("No teams.json found.")
        return False
    return True


def _load_teams() -> dict[str, dict]:
    teams_file = _SCRIPT_DIR / "teams.json"
    if not teams_file.exists():
        return {}
    try:
        return json.loads(teams_file.read_text())
    except (json.JSONDecodeError, OSError):
        return {}


def _select_team(teams: dict[str, dict], team_arg: str | None) -> list[tuple[str, dict]]:
    names = list(teams.keys())

    if team_arg:
        if team_arg.lower() == "all":
            return [(n, teams[n]) for n in names]
        for name in names:
            if team_arg.lower() in name.lower():
                return [(name, teams[name])]
        print(f"Team '{team_arg}' not found.")
        sys.exit(1)

    if len(names) == 1:
        print(f"Team: {names[0]}")
        return [(names[0], teams[names[0]])]

    print("\nAvailable teams:")
    for i, name in enumerate(names, 1):
        print(f"  {i}. {name}")
    while True:
        try:
            choice = input("\nSelect team: ").strip()
            idx = int(choice) - 1
            if 0 <= idx < len(names):
                return [(names[idx], teams[names[idx]])]
        except (ValueError, EOFError):
            sys.exit(1)


def _collect_github_prs(repos: list[str], roster_names: list[str]) -> list[dict]:
    import subprocess

    prs: list[dict] = []
    for repo in repos:
        cmd = [
            "gh",
            "pr",
            "list",
            "--repo",
            repo,
            "--state",
            "open",
            "--json",
            "number,title,author,createdAt,url,reviews,isDraft,commits",
            "--limit",
            "100",
        ]
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
        if proc.returncode != 0 or not proc.stdout.strip():
            continue
        try:
            data = json.loads(proc.stdout)
        except json.JSONDecodeError:
            continue

        for pr in data:
            if pr.get("isDraft"):
                continue
            author = pr.get("author", {}).get("login", "")
            created = pr.get("createdAt", "")
            age = 0
            if created:
                try:
                    dt = datetime.fromisoformat(created.replace("Z", "+00:00"))
                    age = max((datetime.now(timezone.utc) - dt).days, 0)
                except ValueError:
                    pass

            prs.append(
                {
                    "repo": repo,
                    "number": pr.get("number"),
                    "title": pr.get("title", ""),
                    "author": author,
                    "url": pr.get("url", ""),
                    "age_days": age,
                    "review_decision": _human_review_decision(pr.get("reviews", []), pr.get("commits", [])),
                    "platform": "github",
                }
            )
    return prs


def _collect_jira_prs(config: dict, auth: tuple[str, str]) -> list[dict]:
    import requests

    base = config["jira_base_url"]
    label = config.get("jira_label", "")
    components = config.get("jira_components", "")

    jql_parts = [
        f'labels = "{label}"',
        'status IN ("In Review", "Review")',
    ]
    if components:
        jql_parts.append(f'component = "{components}"')

    session = requests.Session()
    session.auth = auth

    try:
        r = session.get(
            f"{base}/rest/api/3/search/jql",
            params={
                "jql": " AND ".join(jql_parts),
                "fields": "customfield_10875,summary,assignee",
                "maxResults": 200,
            },
            timeout=30,
        )
        r.raise_for_status()
    except Exception as e:
        log.warning("JIRA query failed: %s", e)
        return []

    prs: list[dict] = []
    import re

    for issue in r.json().get("issues", []):
        fields = issue.get("fields", {})
        pr_field = fields.get("customfield_10875")
        if not pr_field:
            continue

        raw = str(pr_field)
        urls = re.findall(r'href="(https?://[^"]+(?:pull|merge_request)[^"]*)"', raw)
        if not urls:
            urls = re.findall(r"(https?://\S+/pull/\d+|https?://\S+/merge_requests/\d+)", raw)

        assignee = fields.get("assignee")
        assignee_name = ""
        if isinstance(assignee, dict):
            assignee_name = assignee.get("displayName", "")

        for url in urls:
            num = url.rstrip("/").split("/")[-1]
            platform = "github" if "github.com" in url else "gitlab"
            repo = ""
            if "github.com" in url:
                parts = url.split("github.com/")[-1].split("/pull/")[0]
                repo = parts
            elif "merge_requests" in url:
                parts = url.split("/-/merge_requests/")[0].split("/")
                repo = "/".join(parts[-2:]) if len(parts) >= 2 else parts[-1]

            prs.append(
                {
                    "repo": repo,
                    "number": int(num) if num.isdigit() else num,
                    "title": fields.get("summary", ""),
                    "author": assignee_name,
                    "url": url,
                    "age_days": 0,
                    "review_decision": "",
                    "platform": platform,
                    "jira_key": issue["key"],
                }
            )

    return prs


def _match_to_roster(author: str, roster: list[dict]) -> str | None:
    if not author:
        return None
    low = author.lower()
    for m in roster:
        name = m["name"].lower()
        if low in name or name in low:
            return m["name"]
        parts = name.split()
        if parts and len(parts[0]) >= 3 and parts[0] in low:
            return m["name"]
    return None


def _dedupe_prs(prs: list[dict]) -> list[dict]:
    by_url: dict[str, dict] = {}
    for pr in prs:
        url = pr.get("url", "")
        if not url:
            continue
        existing = by_url.get(url)
        if existing is None:
            by_url[url] = pr
        else:
            if pr.get("age_days", 0) > existing.get("age_days", 0):
                jira_key = existing.get("jira_key") or pr.get("jira_key")
                pr["jira_key"] = jira_key
                by_url[url] = pr
            elif existing.get("age_days", 0) == 0 and pr.get("jira_key"):
                existing["jira_key"] = pr["jira_key"]
    return list(by_url.values())


def _pr_link(pr: dict) -> str:
    url = pr.get("url", "")
    num = pr.get("number", "?")
    platform = pr.get("platform", "github")
    prefix = "#" if platform == "github" else "!"
    return f"[{prefix}{num}]({url})"


_KNOWN_BOT_LOGINS = {
    "coderabbitai",
    "dependabot",
    "renovate",
}


def _extend_bot_logins(extra: list[str]) -> None:
    """# ponytail: extend module-level bot set once per run from config"""
    for b in extra:
        _KNOWN_BOT_LOGINS.add(b.lower())


def _is_bot(login: str) -> bool:
    login_lower = (login or "").lower()
    return "[bot]" in login_lower or login_lower in _KNOWN_BOT_LOGINS


def _human_review_decision(reviews: list[dict], commits: list[dict] | None = None) -> str:
    """Latest APPROVED/CHANGES_REQUESTED por reviewer humano (ignora bots).
    Se houve CHANGES_REQUESTED mas foi feito commit APÓS o último CR (mudanças endereçadas,
    aguardando re-review) -> '' (=> Waiting for Review).
    Retorna 'CHANGES_REQUESTED', 'APPROVED' ou ''."""
    latest: dict[str, dict] = {}
    for r in reviews or []:
        login = (r.get("author") or {}).get("login", "")
        if not login or _is_bot(login):
            continue
        state = r.get("state", "")
        if state in ("APPROVED", "CHANGES_REQUESTED"):
            latest[login] = {"state": state, "at": r.get("submittedAt", "")}
    states = {v["state"] for v in latest.values()}
    if "CHANGES_REQUESTED" in states:
        last_cr = max(v["at"] for v in latest.values() if v["state"] == "CHANGES_REQUESTED")
        last_commit = commits[-1].get("committedDate", "") if commits else ""
        if last_commit and last_commit > last_cr:
            return ""
        return "CHANGES_REQUESTED"
    if "APPROVED" in states:
        return "APPROVED"
    return ""


def _review_status(decision: str) -> str:
    upper = (decision or "").upper()
    if upper == "APPROVED":
        return "Approved"
    if upper == "CHANGES_REQUESTED":
        return "Changes Requested"
    return "Waiting for Review"


def _generate_report(team_name: str, roster: list[dict], prs: list[dict]) -> str:
    now = datetime.now(timezone.utc)
    lines: list[str] = []

    lines.append(f"# PRs Tracker Report — {team_name}")
    lines.append(f"Generated: {now.strftime('%d/%m/%Y — %H:%M:%S')} UTC")
    lines.append(f"Open PRs: {len(prs)}")
    lines.append("")

    # Group by roster member
    by_member: dict[str, list[dict]] = {}
    unmatched: list[dict] = []

    for pr in prs:
        matched = _match_to_roster(pr["author"], roster)
        if matched:
            by_member.setdefault(matched, []).append(pr)
        else:
            unmatched.append(pr)

    lines.append("## Open PRs by Team Member")
    lines.append("")

    for member in roster:
        name = member["name"]
        member_prs = by_member.get(name, [])
        lines.append(f"### {name} ({len(member_prs)} PRs)")
        lines.append("")
        if not member_prs:
            lines.append("No open PRs.")
        else:
            lines.append("| PR | Repo | Title | Age | Review |")
            lines.append("| --- | --- | --- | --- | --- |")
            for pr in sorted(member_prs, key=lambda p: -p.get("age_days", 0)):
                jira = f" ({pr['jira_key']})" if pr.get("jira_key") else ""
                review = pr.get("review_decision", "") or "—"
                lines.append(
                    f"| {_pr_link(pr)} | {pr['repo']} | {pr['title'][:60]}{jira} | {pr['age_days']}d | {review} |"
                )
        lines.append("")

    if unmatched:
        lines.append("### External / Unmatched")
        lines.append("")
        lines.append("| PR | Repo | Author | Title | Age |")
        lines.append("| --- | --- | --- | --- | --- |")
        for pr in sorted(unmatched, key=lambda p: -p.get("age_days", 0)):
            lines.append(f"| {_pr_link(pr)} | {pr['repo']} | {pr['author']} | {pr['title'][:60]} | {pr['age_days']}d |")
        lines.append("")

    # PR age table
    lines.append("## PR Age Summary")
    lines.append("")
    lines.append("| PR | Repo | Author | Title | Days Open |")
    lines.append("| --- | --- | --- | --- | --- |")
    for pr in sorted(prs, key=lambda p: -p.get("age_days", 0)):
        matched = _match_to_roster(pr["author"], roster) or pr["author"]
        lines.append(f"| {_pr_link(pr)} | {pr['repo']} | {matched} | {pr['title'][:50]} | {pr['age_days']}d |")
    lines.append("")

    # Slack MSG — JSON array for workflow consumption (roster members only)
    slack_items = []
    for pr in sorted(prs, key=lambda p: -p.get("age_days", 0)):
        matched = _match_to_roster(pr["author"], roster)
        if not matched:
            continue
        num = pr.get("number", "?")
        platform = pr.get("platform", "github")
        prefix = "#" if platform == "github" else "!"
        slack_items.append(
            {
                "text": f"{prefix}{num} - {pr['title'][:60]}{'...' if len(pr['title']) > 60 else ''}",
                "url": pr.get("url", ""),
                "age": f"{pr.get('age_days', 0)}d",
                "author": matched,
                "status": _review_status(pr.get("review_decision", "")),
            }
        )
    lines.append("## Slack MSG")
    lines.append("")
    lines.append("```json")
    lines.append(json.dumps(slack_items))
    lines.append("```")
    lines.append("")

    return "\n".join(lines)


def _verify_open_prs(jira_sourced: list[dict]) -> set[str]:
    """Verify jira-sourced PRs are open+non-draft. Mutates age_days/review_decision on kept PRs; returns URLs to drop."""
    import subprocess

    drop_urls: set[str] = set()
    for pr in jira_sourced:
        try:
            url = pr["url"]
            if pr.get("platform") == "github":
                proc = subprocess.run(
                    ["gh", "pr", "view", url, "--json", "state,isDraft,createdAt,reviews,commits"],
                    capture_output=True,
                    text=True,
                    timeout=15,
                )
                if proc.returncode != 0:
                    log.info("Dropping unverifiable PR %s", url)
                    drop_urls.add(url)
                    continue
                data = json.loads(proc.stdout)
                if data.get("state", "").upper() != "OPEN":
                    drop_urls.add(url)
                    continue
                if data.get("isDraft"):
                    drop_urls.add(url)
                    continue
                created = data.get("createdAt", "")
                if created:
                    dt = datetime.fromisoformat(created.replace("Z", "+00:00"))
                    pr["age_days"] = max((datetime.now(timezone.utc) - dt).days, 0)
                pr["review_decision"] = _human_review_decision(data.get("reviews", []), data.get("commits", []))
            elif pr.get("platform") == "gitlab":
                proc = subprocess.run(
                    ["glab", "mr", "view", url, "--output", "json"],
                    capture_output=True,
                    text=True,
                    timeout=15,
                )
                if proc.returncode != 0:
                    log.info("Dropping unverifiable PR %s", url)
                    drop_urls.add(url)
                    continue
                data = json.loads(proc.stdout)
                state = data.get("state", "").lower()
                if state != "opened":
                    drop_urls.add(url)
                    continue
                if data.get("draft") or data.get("work_in_progress"):
                    drop_urls.add(url)
                    continue
                created = data.get("created_at", "")
                if created:
                    dt = datetime.fromisoformat(created.replace("Z", "+00:00"))
                    pr["age_days"] = max((datetime.now(timezone.utc) - dt).days, 0)
        except Exception:
            log.info("Dropping unverifiable PR %s", url)
            drop_urls.add(url)

    return drop_urls


def _run_one(team_name: str, sources: dict) -> None:
    from config import load_config

    print(f"\nLoading config for {team_name}...", flush=True)
    config = load_config(sources=sources)

    if config.bot_logins:
        _extend_bot_logins(config.bot_logins)

    if not config.team_name:
        print("Failed to load team config.")
        sys.exit(1)

    auth = (os.environ["JIRA_EMAIL"], os.environ["JIRA_API_TOKEN"])
    jira_base = os.environ["JIRA_BASE_URL"]

    roster_names = [r.name for r in config.roster]
    roster_dicts = [{"name": r.name, "role": r.role} for r in config.roster]
    github_repos = []
    import re

    for _stream, url in config.repo_mapping.items():
        m = re.match(r"https://github\.com/(.+)", url)
        if m:
            github_repos.append(m.group(1))

    cfg = {
        "jira_base_url": jira_base,
        "jira_label": config.jira_label,
        "jira_components": config.jira_components[0] if config.jira_components else "",
    }

    # Collect in parallel
    print("Collecting PRs from GitHub and JIRA...", flush=True)
    all_prs: list[dict] = []

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = {
            pool.submit(_collect_github_prs, github_repos, roster_names): "github",
            pool.submit(_collect_jira_prs, cfg, auth): "jira",
        }
        for future in as_completed(futures):
            source = futures[future]
            try:
                result = future.result()
                print(f"  {source}: {len(result)} PRs found")
                all_prs.extend(result)
            except Exception as e:
                log.warning("Collection from %s failed: %s", source, e)

    # Dedupe
    prs = _dedupe_prs(all_prs)
    print(f"Total unique open PRs: {len(prs)}")

    # Enrich JIRA-sourced PRs: verify they're open and fetch age
    jira_sourced = [pr for pr in prs if pr.get("age_days", 0) == 0 and pr.get("url")]
    if jira_sourced:
        print(f"  Verifying state for {len(jira_sourced)} JIRA-sourced PRs...", flush=True)
        drop_urls = _verify_open_prs(jira_sourced)
        if drop_urls:
            prs = [pr for pr in prs if pr.get("url") not in drop_urls]
            print(f"  Filtered out {len(drop_urls)} closed/merged/draft/unverifiable PRs")

    # Generate report
    print("Generating report...", flush=True)
    report = _generate_report(config.team_name, roster_dicts, prs)

    # Save
    now = datetime.now(timezone.utc)
    month_name = now.strftime("%B")
    year = now.strftime("%Y")
    timestamp = now.strftime("%d-%m-%Y - %H:%M:%S")

    out_dir = _SCRIPT_DIR / "Team-PRs-Report" / year / month_name
    out_dir.mkdir(parents=True, exist_ok=True)

    filename = f"[{timestamp}] - PRs Tracker Report.md"
    out_path = out_dir / filename
    out_path.write_text(report, encoding="utf-8")
    print(f"\nSaved: {out_path}")


def run(team_arg: str | None = None, verbose: bool = False) -> None:
    teams = _load_teams()
    for team_name, sources in _select_team(teams, team_arg):
        _run_one(team_name, sources)


def main() -> None:
    parser = argparse.ArgumentParser(description="Generate team PRs tracker report")
    parser.add_argument("--team", type=str, default=None, help="Team name (substring match)")
    parser.add_argument("--verbose", "-v", action="store_true", help="Debug logging")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)s: %(message)s",
    )

    if not _check_setup():
        print("\nSetup is incomplete. Run:")
        print("  python main.py --setup")
        answer = input("\nRun setup now? (y/N) ").strip().lower()
        if answer == "y":
            from setup import run_setup

            run_setup()
            if not _check_setup():
                print("Setup still incomplete.")
                sys.exit(1)
        else:
            sys.exit(1)

    run(team_arg=args.team, verbose=args.verbose)


if __name__ == "__main__":
    main()
