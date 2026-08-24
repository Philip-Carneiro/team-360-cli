"""Interactive env var setup assistant."""

from __future__ import annotations

import json
import logging
import os
import subprocess
from pathlib import Path

_SCRIPT_DIR = Path(__file__).resolve().parent
_TEAMS_FILE = _SCRIPT_DIR / "teams.json"

log = logging.getLogger(__name__)

ENV_VARS = [
    ("JIRA_BASE_URL", "JIRA instance URL (e.g. https://your-org.atlassian.net)", True),
    ("JIRA_EMAIL", "Atlassian account email", True),
    ("JIRA_API_TOKEN", "JIRA REST API token", True),
    ("CONFLUENCE_URL", "Confluence base URL (usually $JIRA_BASE_URL/wiki)", True),
    ("CONFLUENCE_USERNAME", "Usually same as JIRA_EMAIL", True),
    ("CONFLUENCE_API_TOKEN", "Usually same as JIRA_API_TOKEN", True),
    ("OBSIDIAN_VAULT", "Path to Obsidian vault for report storage", False),
    ("GITLAB_HOST", "GitLab hostname (e.g. gitlab.example.com)", False),
    ("GITLAB_TOKEN", "GitLab API token", False),
    ("GOOGLE_CLIENT_ID", "Google OAuth client ID (for Calendar API)", False),
    ("GOOGLE_CLIENT_SECRET", "Google OAuth client secret", False),
    ("GOOGLE_REFRESH_TOKEN", "Google OAuth refresh token", False),
]


def _verify_jira(base_url: str, email: str, token: str) -> bool:
    try:
        import requests

        r = requests.get(
            f"{base_url.rstrip('/')}/rest/api/3/myself",
            auth=(email, token),
            timeout=10,
        )
        if r.ok:
            print(f"  JIRA: OK — logged in as {r.json().get('displayName', email)}")
            return True
        print(f"  JIRA: FAILED — HTTP {r.status_code}")
    except Exception as e:
        print(f"  JIRA: FAILED — {e}")
    return False


def _verify_confluence(base_url: str, username: str, token: str) -> bool:
    try:
        import requests

        r = requests.get(
            f"{base_url.rstrip('/')}/rest/api/content?limit=1",
            auth=(username, token),
            timeout=10,
        )
        if r.ok:
            print("  Confluence: OK")
            return True
        print(f"  Confluence: FAILED — HTTP {r.status_code}")
    except Exception as e:
        print(f"  Confluence: FAILED — {e}")
    return False


def _verify_gitlab() -> bool:
    try:
        result = subprocess.run(
            ["glab", "auth", "status"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        if result.returncode == 0:
            print("  GitLab: OK")
            return True
        print(f"  GitLab: FAILED — {result.stderr.strip()}")
    except FileNotFoundError:
        print("  GitLab: SKIPPED — glab CLI not found")
    except Exception as e:
        print(f"  GitLab: FAILED — {e}")
    return False


def _verify_github() -> bool:
    try:
        result = subprocess.run(
            ["gh", "auth", "status"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        if result.returncode == 0:
            print("  GitHub CLI: OK")
            return True
        print(f"  GitHub CLI: FAILED — {result.stderr.strip()}")
    except FileNotFoundError:
        print("  GitHub CLI: SKIP — gh not installed")
    except Exception as e:
        print(f"  GitHub CLI: FAILED — {e}")
    return False


def _verify_google_calendar(roster: list[dict], calendar_ids: list[str]) -> bool:
    """Verify Google Calendar access and check for PTO events."""
    from datetime import datetime, timedelta, timezone

    import requests

    from collectors.calendar import _fetch_events, _load_google_creds

    creds = _load_google_creds()
    if not creds:
        print("  Google Calendar: SKIP — no credentials (env vars or token file)")
        return False

    client_id, client_secret, refresh_token = creds
    try:
        r = requests.post(
            "https://oauth2.googleapis.com/token",
            data={
                "client_id": client_id,
                "client_secret": client_secret,
                "refresh_token": refresh_token,
                "grant_type": "refresh_token",
            },
            timeout=15,
        )
        r.raise_for_status()
        token = r.json().get("access_token")
        if not token:
            print("  Google Calendar: FAILED — no access token in response")
            return False
        print("  Google Calendar: OK — token refreshed")
    except Exception as e:
        print(f"  Google Calendar: FAILED — {e}")
        return False

    if not calendar_ids:
        print("  Google Calendar: WARN — no PTO calendar IDs configured in team page")
        return True

    now = datetime.now(timezone.utc)
    time_min = (now - timedelta(days=30)).strftime("%Y-%m-%dT00:00:00Z")
    time_max = now.strftime("%Y-%m-%dT23:59:59Z")

    for cal_id in calendar_ids:
        events = _fetch_events(token, cal_id, time_min, time_max)
        short_id = cal_id[:30] + ("..." if len(cal_id) > 30 else "")
        print(f"    Calendar {short_id}: {len(events)} events (last 30 days)")

    return True


def _verify_team_config(team_name: str, sources: dict[str, str]) -> dict:
    """Load and verify a team's config. Returns parsed config dict or empty."""
    from config import load_config

    results = {"config": None, "pages_ok": True, "calendar_ids": []}

    # Verify Confluence pages
    print(f"\n  --- Confluence pages for '{team_name}' ---")
    for key, label, _ in TEAM_FIELDS:
        value = sources.get(key, "")
        if value and value != "local":
            ok = _verify_confluence_page(value, label)
            if not ok:
                results["pages_ok"] = False
        elif value == "local":
            print(f"    {label}: local")
        else:
            print(f"    {label}: NOT SET")
            results["pages_ok"] = False

    # Try loading the full config
    print(f"\n  --- Config parsing for '{team_name}' ---")
    try:
        config = load_config(sources=sources)
        results["config"] = config
        print(f"    Team name: {config.team_name or 'NOT FOUND'}")
        print(f"    Roster: {len(config.roster)} members")
        print(f"    Boards: {len(config.boards)}")
        print(f"    JIRA label: {config.jira_label or 'NOT SET'}")
        print(f"    JIRA projects: {', '.join(config.jira_projects) if config.jira_projects else 'NOT SET'}")
        print(f"    PTO calendars: {len(config.pto_calendar_ids)}")
        results["calendar_ids"] = config.pto_calendar_ids

        if not config.team_name:
            print("    WARN — team_md page did not contain a Team field")
        if not config.roster:
            print("    WARN — no roster found, workload analysis will be empty")
        if not config.boards:
            print("    WARN — no boards found, cannot collect board data")
        if not config.jira_label:
            print("    WARN — no JIRA label, JQL queries will return all tickets")
    except Exception as e:
        print(f"    FAILED to parse config: {e}")

    return results


def run_check() -> None:
    """Verify all connections, team config, and calendar access."""
    print("\n=== 360 CLI — Health Check ===\n")

    passed = 0
    failed = 0
    skipped = 0

    def _mark(ok: bool | None):
        nonlocal passed, failed, skipped
        if ok is None:
            skipped += 1
        elif ok:
            passed += 1
        else:
            failed += 1

    # 1. Environment variables
    print("--- Environment Variables ---")
    required = ["JIRA_BASE_URL", "JIRA_EMAIL", "JIRA_API_TOKEN"]
    optional = [
        "CONFLUENCE_URL",
        "CONFLUENCE_USERNAME",
        "CONFLUENCE_API_TOKEN",
        "GITLAB_HOST",
        "OBSIDIAN_VAULT",
        "GOOGLE_CLIENT_ID",
        "GOOGLE_CLIENT_SECRET",
        "GOOGLE_REFRESH_TOKEN",
    ]
    for var in required:
        val = os.environ.get(var, "")
        status = "OK" if val else "MISSING (required)"
        print(f"  {var}: {status}")
        _mark(bool(val))
    for var in optional:
        val = os.environ.get(var, "")
        if val:
            print(f"  {var}: OK")
        else:
            print(f"  {var}: not set (optional)")

    # 2. Service connectivity
    print("\n--- Service Connectivity ---")
    jira_url = os.environ.get("JIRA_BASE_URL", "")
    jira_email = os.environ.get("JIRA_EMAIL", "")
    jira_token = os.environ.get("JIRA_API_TOKEN", "")
    conf_url = os.environ.get("CONFLUENCE_URL", f"{jira_url}/wiki" if jira_url else "")
    conf_user = os.environ.get("CONFLUENCE_USERNAME", jira_email)
    conf_token = os.environ.get("CONFLUENCE_API_TOKEN", jira_token)

    if jira_url and jira_email and jira_token:
        _mark(_verify_jira(jira_url, jira_email, jira_token))
    else:
        print("  JIRA: SKIP — missing credentials")
        _mark(None)

    if conf_url and conf_user and conf_token:
        _mark(_verify_confluence(conf_url, conf_user, conf_token))
    else:
        print("  Confluence: SKIP — missing credentials")
        _mark(None)

    _mark(_verify_github())

    if os.environ.get("GITLAB_HOST"):
        _mark(_verify_gitlab())
    else:
        print("  GitLab: SKIP — GITLAB_HOST not set")

    # 3. teams.json
    print("\n--- Team Configuration ---")
    teams = _load_teams()
    if not teams:
        print("  teams.json: NOT FOUND — run --setup or --add-team")
        _mark(False)
    else:
        print(f"  teams.json: OK — {len(teams)} team(s)")
        _mark(True)

        for team_name, sources in teams.items():
            result = _verify_team_config(team_name, sources)
            _mark(result["pages_ok"])

            # 4. Google Calendar per team
            roster = []
            if result["config"] and result["config"].roster:
                roster = [{"name": r.name, "role": r.role} for r in result["config"].roster]
            print(f"\n  --- Google Calendar for '{team_name}' ---")
            _mark(_verify_google_calendar(roster, result["calendar_ids"]))

    # 5. Obsidian vault
    vault = os.environ.get("OBSIDIAN_VAULT", "")
    print("\n--- Obsidian Vault ---")
    if vault:
        from pathlib import Path

        vault_path = Path(vault)
        if vault_path.exists():
            print(f"  Vault: OK — {vault_path}")
            _mark(True)
        else:
            print(f"  Vault: FAILED — path does not exist: {vault_path}")
            _mark(False)
    else:
        print("  Vault: SKIP — OBSIDIAN_VAULT not set")

    # Summary
    total = passed + failed + skipped
    print(f"\n=== Results: {passed}/{total} passed, {failed} failed, {skipped} skipped ===")
    if failed == 0:
        print("All checks passed. Ready to generate reports.")
    else:
        print("Fix the failures above, then run --check again.")
    print()


def run_setup() -> None:
    """Interactive env var setup wizard."""
    print("\n=== 360 CLI — Environment Setup ===\n")

    exports: list[str] = []
    values: dict[str, str] = {}

    for var, desc, required in ENV_VARS:
        current = os.environ.get(var, "")
        tag = "(required)" if required else "(optional)"
        if current:
            print(f"  {var}: already set")
            values[var] = current
            continue

        value = input(f"  {var} {tag}\n  {desc}\n  > ").strip()
        if not value:
            if required:
                print(f"  WARNING: {var} is required but was left empty")
            continue

        values[var] = value
        exports.append(f'export {var}="{value}"')
        os.environ[var] = value

    if not exports:
        print("\nAll variables already set.")
    else:
        print("\n--- Add to ~/.zshrc ---")
        block = "\n".join(exports)
        print(block)
        print("---")

        answer = input("\nAppend these to ~/.zshrc? (y/N) ").strip().lower()
        if answer == "y":
            zshrc = Path.home() / ".zshrc"
            with open(zshrc, "a") as f:
                f.write(f"\n# team-360-cli\n{block}\n")
            print(f"Appended to {zshrc}")

    # Verify connectivity
    print("\n--- Verifying connectivity ---")
    if values.get("JIRA_BASE_URL") and values.get("JIRA_EMAIL") and values.get("JIRA_API_TOKEN"):
        _verify_jira(values["JIRA_BASE_URL"], values["JIRA_EMAIL"], values["JIRA_API_TOKEN"])
    else:
        print("  JIRA: SKIPPED — missing credentials")

    if values.get("CONFLUENCE_URL") and values.get("CONFLUENCE_USERNAME") and values.get("CONFLUENCE_API_TOKEN"):
        _verify_confluence(values["CONFLUENCE_URL"], values["CONFLUENCE_USERNAME"], values["CONFLUENCE_API_TOKEN"])
    else:
        print("  Confluence: SKIPPED — missing credentials")

    if values.get("GITLAB_HOST"):
        _verify_gitlab()
    else:
        print("  GitLab: SKIPPED — not configured")

    print("\nSetup complete.\n")

    # Check if teams.json exists
    if _TEAMS_FILE.exists():
        teams = _load_teams()
        if teams:
            print(f"teams.json found with {len(teams)} team(s): {', '.join(teams.keys())}")
            answer = input("Would you like to add or update a team? (y/N) ").strip().lower()
        else:
            print("teams.json found but empty.")
            answer = input("Would you like to add a team? (y/N) ").strip().lower()
    else:
        print("No teams.json found — you need at least one team to generate reports.")
        answer = input("Add a team now? (Y/n) ").strip().lower()
        if answer == "" or answer == "y":
            answer = "y"

    if answer == "y":
        add_team()


def _load_teams() -> dict[str, dict]:
    if _TEAMS_FILE.exists():
        try:
            return json.loads(_TEAMS_FILE.read_text())
        except (json.JSONDecodeError, OSError):
            return {}
    return {}


def _save_teams(teams: dict[str, dict]) -> None:
    _TEAMS_FILE.write_text(json.dumps(teams, indent=2) + "\n")
    print(f"  Saved to {_TEAMS_FILE}")


TEAM_FIELDS = [
    ("team_md", "Team overview page", "Confluence page with roster, boards, JIRA label, cadence"),
    ("jira_md", "JIRA reference page", "Confluence page with JIRA projects, JQL templates, repo mapping"),
    ("confluence_md", "Confluence reference page", "Confluence page with 360 report folder IDs"),
]


def _extract_page_id(value: str) -> str | None:
    """Extract Confluence page ID from URL or bare ID."""
    import re

    m = re.search(r"/pages/(\d+)", value)
    if m:
        return m.group(1)
    if value.strip().isdigit():
        return value.strip()
    return None


def _verify_confluence_page(url_or_id: str, label: str) -> bool:
    """Verify a Confluence page is accessible. Returns True if OK."""
    import requests

    page_id = _extract_page_id(url_or_id)
    if not page_id:
        if url_or_id == "local":
            print(f"    {label}: local (no verification needed)")
            return True
        print(f"    {label}: SKIP — could not extract page ID from '{url_or_id}'")
        return False

    base = os.environ.get("CONFLUENCE_URL") or os.environ.get("JIRA_BASE_URL", "").rstrip("/") + "/wiki"
    user = os.environ.get("CONFLUENCE_USERNAME") or os.environ.get("JIRA_EMAIL", "")
    token = os.environ.get("CONFLUENCE_API_TOKEN") or os.environ.get("JIRA_API_TOKEN", "")

    if not all([base, user, token]):
        print(f"    {label}: SKIP — Confluence credentials not set")
        return False

    try:
        r = requests.get(
            f"{base.rstrip('/')}/api/v2/pages/{page_id}",
            auth=(user, token),
            timeout=15,
        )
        if r.ok:
            title = r.json().get("title", "")
            print(f"    {label}: OK — '{title}'")
            return True
        print(f"    {label}: FAILED — HTTP {r.status_code}")
    except Exception as e:
        print(f"    {label}: FAILED — {e}")
    return False


def add_team() -> None:
    """Interactive team configuration wizard."""
    teams = _load_teams()

    while True:
        print("\n=== Add Team ===\n")

        if teams:
            print("Existing teams:")
            for name in teams:
                print(f"  - {name}")
            print()

        team_name = input("Team name: ").strip()
        if not team_name:
            print("Team name cannot be empty.")
            continue

        if team_name in teams:
            overwrite = input(f"  '{team_name}' already exists. Overwrite? (y/N) ").strip().lower()
            if overwrite != "y":
                continue

        sources: dict[str, str] = {}
        print(f"\nConfiguring '{team_name}':")
        print("  For each field, enter a Confluence page URL, a page ID, or 'local' to read from disk.\n")

        for key, label, desc in TEAM_FIELDS:
            existing = teams.get(team_name, {}).get(key, "")
            hint = f" (current: {existing})" if existing else ""
            value = input(f"  {label}{hint}\n  {desc}\n  > ").strip()
            if value:
                sources[key] = value
            elif existing:
                sources[key] = existing
                print(f"  Keeping: {existing}")

        # JIRA projects (optional override — normally parsed from jira_md page)
        existing_projects = teams.get(team_name, {}).get("jira_projects", "")
        hint = f" (current: {existing_projects})" if existing_projects else ""
        print(f"\n  JIRA project keys{hint}")
        print("  Comma-separated project keys used in JQL queries (e.g. MYPROJ,OTHERPROJ)")
        print("  Leave empty if your jira_md page already lists them")
        projects = input("  > ").strip()
        if projects:
            sources["jira_projects"] = projects
        elif existing_projects:
            sources["jira_projects"] = existing_projects

        if not sources:
            print("  No fields provided, skipping.")
            again = input("\nAdd another team? (y/N) ").strip().lower()
            if again != "y":
                break
            continue

        # Verify Confluence page access
        print(f"\n  --- Verifying Confluence pages for '{team_name}' ---")
        all_ok = True
        for key, label, _ in TEAM_FIELDS:
            if key in sources and sources[key] != "local":
                ok = _verify_confluence_page(sources[key], label)
                if not ok:
                    all_ok = False

        if not all_ok:
            proceed = input("\n  Some pages could not be verified. Save anyway? (y/N) ").strip().lower()
            if proceed != "y":
                print("  Team not saved.")
                again = input("\nAdd another team? (y/N) ").strip().lower()
                if again != "y":
                    break
                continue

        teams[team_name] = sources
        _save_teams(teams)
        print(f"\n  Team '{team_name}' configured.")

        again = input("\nAdd another team? (y/N) ").strip().lower()
        if again != "y":
            break

    if teams:
        print(f"\nTeams in {_TEAMS_FILE.name}:")
        for name in teams:
            print(f"  - {name}")
    print()
