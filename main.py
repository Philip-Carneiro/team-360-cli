#!/usr/bin/env python3
"""360-degree team status report generator — zero-token CLI."""

from __future__ import annotations

import argparse
import json
import logging
import os
import select
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path

log = logging.getLogger("team360")

_SCRIPT_DIR = Path(__file__).resolve().parent


def _load_teams() -> dict[str, dict]:
    """Load teams.json from the script directory."""
    teams_file = _SCRIPT_DIR / "teams.json"
    if not teams_file.exists():
        return {}
    try:
        return json.loads(teams_file.read_text())
    except (json.JSONDecodeError, OSError) as e:
        log.warning("Failed to parse teams.json: %s", e)
        return {}


_TEAM_TIMEOUT = 60  # seconds before defaulting to "all"


def _select_team(teams: dict[str, dict], team_arg: str | None) -> list[tuple[str, dict]]:
    """Select team(s) from the config. Returns list of (team_name, sources_dict)."""
    names = list(teams.keys())

    if team_arg:
        if team_arg.lower() == "all":
            return [(n, teams[n]) for n in names]
        for name in names:
            if team_arg.lower() in name.lower():
                return [(name, teams[name])]
        print(f"Team '{team_arg}' not found in teams.json.")
        sys.exit(1)

    all_idx = len(names) + 1
    print("\nAvailable teams:")
    for i, name in enumerate(names, 1):
        print(f"  {i}. {name}")
    print(f"  {all_idx}. All teams")

    while True:
        choice = _input_with_timeout(
            f"\nSelect team [1-{all_idx}] (defaults to All in {_TEAM_TIMEOUT}s): ",
            _TEAM_TIMEOUT,
        )
        if choice is None:
            print(f"\n\n⏱  No selection after {_TEAM_TIMEOUT}s — running for all teams")
            return [(n, teams[n]) for n in names]
        try:
            idx = int(choice)
        except ValueError:
            print(f"Pick a number between 1 and {all_idx}.")
            continue
        if idx == all_idx:
            return [(n, teams[n]) for n in names]
        if 1 <= idx <= len(names):
            return [(names[idx - 1], teams[names[idx - 1]])]
        print(f"Pick a number between 1 and {all_idx}.")


_SWIMLANE_TIMEOUT = 60  # seconds before defaulting to "all"


def _input_with_timeout(prompt: str, timeout: int) -> str | None:
    """Read input with a timeout. Returns None on timeout or EOFError."""
    print(prompt, end="", flush=True)
    ready, _, _ = select.select([sys.stdin], [], [], timeout)
    if ready:
        return sys.stdin.readline().strip()
    return None


def _select_swimlane(swimlanes: list[dict]) -> str | None:
    """Present swimlane selection. Returns parent_key or None for all."""
    if not swimlanes:
        return None

    total = sum(s["count"] for s in swimlanes)
    all_idx = len(swimlanes) + 1

    print("\nSwimlanes (parent Epics):")
    for i, sl in enumerate(swimlanes, 1):
        print(f"  {i}. {sl['key']} — {sl['name']} ({sl['count']} tickets)")
    print(f"  {all_idx}. All swimlanes ({total} tickets)")

    while True:
        choice = _input_with_timeout(
            f"\nSelect swimlane [1-{all_idx}] (defaults to All in {_SWIMLANE_TIMEOUT}s): ",
            _SWIMLANE_TIMEOUT,
        )
        if choice is None:
            print(f"\n\n⏱  No selection after {_SWIMLANE_TIMEOUT}s — running with All swimlanes ({total} tickets)")
            return None
        try:
            idx = int(choice)
        except ValueError:
            print(f"Pick a number between 1 and {all_idx}.")
            continue
        if idx == all_idx:
            return None
        if 1 <= idx <= len(swimlanes):
            selected = swimlanes[idx - 1]
            print(f"Filtering to: {selected['key']} — {selected['name']}")
            return selected["key"]
        print(f"Pick a number between 1 and {all_idx}.")


def _swimlane_to_jql(swimlane: str) -> tuple[str, str]:
    """Convert swimlane string (possibly comma-separated) to JQL and display name."""
    parts = [s.strip() for s in swimlane.split(",") if s.strip()]
    if len(parts) == 1:
        return (f'fixVersion = "{parts[0]}"', parts[0])
    quoted = ", ".join(f'"{p}"' for p in parts)
    return (f"fixVersion IN ({quoted})", ", ".join(parts))


def _jira_auth() -> tuple[str, str]:
    return (os.environ["JIRA_EMAIL"], os.environ["JIRA_API_TOKEN"])


def _confluence_auth() -> tuple[str, str]:
    return (os.environ["CONFLUENCE_USERNAME"], os.environ["CONFLUENCE_API_TOKEN"])


def _check_credentials() -> bool:
    required = ["JIRA_BASE_URL", "JIRA_EMAIL", "JIRA_API_TOKEN"]
    missing = [v for v in required if not os.environ.get(v)]
    if missing:
        log.error("Missing env vars: %s — run with --setup to configure", ", ".join(missing))
        return False
    return True


def _find_prev_report_text() -> str | None:
    """Find most recent non-test 360 report from vault or local dir for delta computation."""
    dirs = []
    vault = os.environ.get("OBSIDIAN_VAULT")
    if vault:
        dirs.append(Path(vault) / "SCRUMBAN" / "STATUS" / "360")
    dirs.append(Path("reports/team-360-status"))

    for d in dirs:
        if not d.exists():
            continue
        files = sorted(
            [f for f in d.iterdir() if f.suffix == ".md" and "-test" not in f.name.lower()],
            key=lambda f: f.stat().st_mtime,
            reverse=True,
        )
        if files:
            log.info("Previous report for delta: %s", files[0].name)
            return files[0].read_text()

    for d in dirs:
        if not d.exists():
            continue
        files = sorted(
            [f for f in d.iterdir() if f.suffix == ".md"],
            key=lambda f: f.stat().st_mtime,
            reverse=True,
        )
        if files:
            log.info("Previous report (test) for delta: %s", files[0].name)
            return files[0].read_text()
    return None


def run(
    workspace: str | None = None,
    test_mode: bool = False,
    suffix: str = "",
    sources: dict[str, str] | None = None,
    swimlane: str | None = "ask",
) -> None:
    from collectors.calendar import collect_absences
    from collectors.github import collect_github_prs
    from collectors.gitlab import collect_gitlab_mrs
    from collectors.jira import (
        collect_backlog,
        collect_board_swimlanes,
        collect_bugs,
        collect_completed,
        collect_doing_board,
        collect_epic_progress,
        collect_learning_tickets,
        collect_strats,
        collect_testing_transitions,
    )
    from config import load_config
    from heuristics import apply_heuristics
    from publishers.confluence import fetch_previous_360
    from publishers.vault import save_to_vault
    from report import generate_report

    # 1. Load config
    print("\nLoading team configuration...", flush=True)
    config = load_config(workspace, sources=sources)
    if not config.team_name:
        log.error("Could not load team config. Check workspace path.")
        sys.exit(1)
    print(f"Team: {config.team_name} ({len(config.roster)} members)")
    log.info("Team: %s (%d members)", config.team_name, len(config.roster))

    # 2. Verify credentials
    if not _check_credentials():
        sys.exit(1)

    auth = _jira_auth()
    conf_auth = _confluence_auth()
    jira_base = os.environ["JIRA_BASE_URL"]
    conf_url = os.environ.get("CONFLUENCE_URL", f"{jira_base}/wiki")

    bugs_filter_id = None
    doing_board_url = None
    doing_board_name = None
    doing_board_id = None
    for b in config.boards:
        if "bug" in b.name.lower():
            import re

            m = re.search(r"filter[=/](\d+)", b.url)
            if m:
                bugs_filter_id = m.group(1)
        if "doing" in b.name.lower() or "scrumban" in b.name.lower():
            doing_board_url = b.url
            doing_board_name = b.name
            doing_board_id = b.board_id

    roster_dicts = [
        {"name": r.name, "role": r.role, "location": getattr(r, "location", ""), "email": getattr(r, "email", "")}
        for r in config.roster
    ]

    repos_list = []
    for stream, url in config.repo_mapping.items():
        import re

        m = re.match(r"https://github\.com/(.+)", url)
        if m:
            repos_list.append({"name": m.group(1), "stream": stream, "platform": "github"})

    cfg = {
        "team_name": config.team_name,
        "jira_label": config.jira_label,
        "jira_components": config.jira_components[0] if config.jira_components else "",
        "jira_base_url": jira_base,
        "roster": roster_dicts,
        "strat_prefix": config.strat_prefix,
        "bot_logins": config.bot_logins,
        "boards": {b.name: {"id": b.board_id, "url": b.url} for b in config.boards},
        "doing_board_url": doing_board_url,
        "doing_board_name": doing_board_name,
        "doing_board_id": doing_board_id,
        "bugs_filter_id": bugs_filter_id,
        "activity_targets": config.activity_targets,
        "strat_boards": bool(config.strats_committed_id),
        "repos": repos_list,
        "github_repos": [r["name"] for r in repos_list if r.get("platform") == "github"],
        "confluence_root_dir_id": config.confluence_root_folder_id,
        "confluence_past_dir_id": config.confluence_past_folder_id,
        "confluence_space_key": config.confluence_space_key,
        "confluence_url": conf_url,
        "pto_calendar_ids": config.pto_calendar_ids,
        "jira_projects": ",".join(config.jira_projects) if config.jira_projects else "",
    }

    # 3. Fetch previous 360 from Confluence
    previous_360 = None
    prev_360_url = None
    prev_360_title = None
    prev_360_date = "2000-01-01"
    if cfg.get("confluence_root_dir_id"):
        try:
            prev_data, prev_url = fetch_previous_360(conf_auth, conf_url, cfg["confluence_root_dir_id"])
            if prev_data:
                previous_360 = prev_data
                prev_360_url = prev_url
                prev_360_title = prev_data.get("title", "")
                title = prev_360_title
                import re

                m = re.search(r"(\d{2})/(\d{2})/(\d{4})", title)
                if m:
                    prev_360_date = f"{m.group(3)}-{m.group(2)}-{m.group(1)}"
                log.info("Previous 360: %s (%s)", title, prev_360_date)
        except Exception as e:
            log.warning("Could not fetch previous 360: %s", e)

    collection_time = datetime.now(timezone.utc).strftime("%H:%M")

    # 4. Swimlane discovery + selection (before data collection)
    swimlane_jql: str | None = None
    swimlane_name: str | None = None

    if swimlane == "all":
        pass
    elif swimlane == "ask":
        print("Fetching swimlanes from board...", flush=True)
        swimlanes = collect_board_swimlanes(cfg, auth, [])
        if swimlanes:
            selected = _select_swimlane(swimlanes)
            if selected:
                sl = next((s for s in swimlanes if s["key"] == selected), None)
                if sl:
                    swimlane_jql = sl["jql"]
                    swimlane_name = sl["name"]
        else:
            print("No swimlanes found — running with all tickets.")
    elif swimlane:
        # Direct swimlane name passed — resolve to JQL
        swimlane_jql, swimlane_name = _swimlane_to_jql(swimlane)

    if swimlane_jql:
        log.info("Swimlane filter: %s (JQL: %s)", swimlane_name, swimlane_jql)

    # 4.1. Collect data in parallel
    print("Collecting data (JIRA, GitHub, GitLab)...", flush=True)

    def _jira_collect():
        doing = collect_doing_board(cfg, auth, prev_360_date, swimlane_jql=swimlane_jql)
        epics = [t for t in doing if t.get("issuetype") == "Epic"]
        return {
            "doing_board": doing,
            "backlog": collect_backlog(cfg, auth),
            "bugs": collect_bugs(cfg, auth),
            "strats": collect_strats(cfg, auth),
            "completed": collect_completed(cfg, auth, prev_360_date),
            "testing_transitions": collect_testing_transitions(cfg, auth, prev_360_date),
            "epic_progress": collect_epic_progress(cfg, auth, epics),
        }

    results = {"jira": None, "github": None, "gitlab": None}

    with ThreadPoolExecutor(max_workers=3) as pool:
        futures = {
            pool.submit(_jira_collect): "jira",
            pool.submit(collect_github_prs, cfg): "github",
            pool.submit(collect_gitlab_mrs, cfg): "gitlab",
        }
        for future in as_completed(futures):
            name = futures[future]
            try:
                results[name] = future.result()
                log.info("Collected: %s", name)
            except Exception as e:
                log.error("Collector %s failed: %s", name, e, exc_info=True)

    jira = results["jira"] or {}
    github = results["github"] or {"roster_prs": [], "external_prs": [], "bot_prs": []}
    gitlab = results["gitlab"] or {"open_mrs": []}

    # 4.5. Collect absences from Google Calendar (PTO + OOO + Sick)
    absence_data: dict[str, dict[str, list[str]]] = {}
    if cfg.get("pto_calendar_ids"):
        try:
            absence_data = collect_absences(
                calendar_ids=cfg["pto_calendar_ids"],
                roster=roster_dicts,
                since_date=prev_360_date,
            )
        except Exception as e:
            log.warning("Absence collection failed: %s", e)

    # 4.7. Collect learning tickets
    learning_tickets: list[dict] = []
    try:
        learning_tickets = collect_learning_tickets(cfg, auth)
        log.info("Learning tickets found: %d", len(learning_tickets))
    except Exception as e:
        log.warning("Learning ticket collection failed: %s", e)

    # 5. Apply heuristics
    print("Analyzing data...", flush=True)
    analyzed = apply_heuristics(
        doing_board=jira.get("doing_board", []),
        backlog=jira.get("backlog", []),
        strats=jira.get("strats", {"committed": [], "planning": []}),
        completed=jira.get("completed", []),
        github_prs=github,
        gitlab_mrs=gitlab,
        testing_transitions=jira.get("testing_transitions", []),
        epic_progress=jira.get("epic_progress", []),
        config=cfg,
        previous_360=previous_360,
        absence_data=absence_data,
        learning_tickets=learning_tickets,
    )
    if swimlane_name:
        analyzed["swimlane_filter"] = swimlane_name
    log.info("Heuristics applied")

    # 5.5 Find previous report text for delta columns
    prev_report_text = _find_prev_report_text()

    # 6. Generate report
    report, emoji = generate_report(
        analyzed=analyzed,
        config=cfg,
        test_mode=test_mode,
        prev_360_url=prev_360_url,
        prev_360_title=prev_360_title,
        collection_time=collection_time,
        prev_report_text=prev_report_text,
    )
    log.info("Report generated (%d chars)", len(report))

    # 7. Save outputs
    print("Saving report...", flush=True)
    today = datetime.now(timezone.utc).strftime("%d-%m-%Y")
    team = config.team_name

    out_dir = Path("reports/team-360-status")
    out_dir.mkdir(parents=True, exist_ok=True)
    file_suffix = "-test" if test_mode else ""
    if suffix:
        file_suffix += f"-{suffix}"
    filename = f"{emoji} {team} 360 - {today}{file_suffix}.md"
    out_path = out_dir / filename
    out_path.write_text(report)
    print(f"Local: {out_path}")

    vault_path = save_to_vault(report, team, today + (f"-{suffix}" if suffix else ""), test_mode, emoji=emoji)
    if vault_path:
        print(f"Vault: {vault_path}")

    # Confluence save
    if cfg.get("confluence_root_dir_id"):
        try:
            from publishers.confluence import _md_to_html, publish_360

            conf_url_base = cfg["confluence_url"]
            root_id = int(cfg["confluence_root_dir_id"])
            space_key = cfg.get("confluence_space_key", "")
            date_str = datetime.now(timezone.utc).strftime("%d/%m/%Y")
            prefix = "test-" if test_mode else ""
            title_suffix = f"-{suffix}" if suffix else ""
            title = f"{emoji} {prefix}{team} 360 - {date_str}{title_suffix}"
            html = _md_to_html(report)
            _, page_url = publish_360(conf_auth, conf_url_base, root_id, title, html, space_key)
            print(f"Confluence: {page_url}")
        except Exception as e:
            msg = str(e)
            if hasattr(e, "response") and hasattr(e.response, "text"):
                msg = f"{e} | Response: {e.response.text[:500]}"
            log.error("Confluence publish failed: %s", msg)
            sys.exit(1)

    if test_mode:
        print(f"\n*** TEST MODE — no archive, suffix {file_suffix} ***")


def main() -> None:
    parser = argparse.ArgumentParser(description="Generate 360-degree team status report (zero tokens)")
    parser.add_argument("--test", action="store_true", help="Test mode — no archive, -test suffix")
    parser.add_argument("--setup", action="store_true", help="Interactive env var setup")
    parser.add_argument("--add-team", action="store_true", help="Add or update a team in teams.json")
    parser.add_argument("--check", action="store_true", help="Verify all connections, team config, and calendars")
    parser.add_argument("--workspace", type=str, default=None, help="Path to team workspace (contains team.md)")
    parser.add_argument("--team", type=str, default=None, help="Team name (substring match against teams.json)")
    parser.add_argument("--suffix", type=str, default="", help="Extra suffix for filename (e.g., '2' for -test-2)")
    parser.add_argument(
        "--swimlane",
        type=str,
        default="ask",
        help="Swimlane filter: 'all' for everything, 'ask' for interactive, or Epic key",
    )
    parser.add_argument("--verbose", "-v", action="store_true", help="Debug logging")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)s: %(message)s",
    )

    if args.setup:
        from setup import run_setup

        run_setup()
        return

    if args.add_team:
        from setup import add_team

        add_team()
        return

    if args.check:
        from setup import run_check

        run_check()
        return

    teams = _load_teams()
    if not teams and not args.workspace:
        print("No teams.json found. Run one of:")
        print("  python main.py --setup       # full setup (credentials + team)")
        print("  python main.py --add-team    # add a team to teams.json")
        print("  python main.py --workspace . # use local markdown files instead")
        sys.exit(1)

    if teams:
        selected = _select_team(teams, args.team)
        for i, (team_name, sources) in enumerate(selected):
            if len(selected) > 1:
                print(f"\n{'=' * 50}")
                print(f"  [{i + 1}/{len(selected)}] Generating report for: {team_name}")
                print(f"{'=' * 50}")
            log.info("Selected team: %s", team_name)
            run(
                workspace=args.workspace,
                test_mode=args.test,
                suffix=args.suffix,
                sources=sources,
                swimlane=args.swimlane,
            )
    else:
        run(workspace=args.workspace, test_mode=args.test, suffix=args.suffix, sources=None, swimlane=args.swimlane)


if __name__ == "__main__":
    main()
