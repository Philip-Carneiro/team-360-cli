"""Team configuration reader — parses markdown files from the workspace."""

from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import unquote

log = logging.getLogger(__name__)


@dataclass
class TeamMember:
    name: str
    role: str
    location: str
    email: str = ""


@dataclass
class BoardInfo:
    name: str
    board_id: str
    url: str


@dataclass
class TeamConfig:
    # team.md
    team_name: str = ""
    manager: str = ""
    charter: str = ""
    jira_label: str = ""
    jira_components: list[str] = field(default_factory=list)
    boards: list[BoardInfo] = field(default_factory=list)
    strats_committed_id: str = ""
    strats_planning_id: str = ""
    roster: list[TeamMember] = field(default_factory=list)
    cadence: list[dict[str, str]] = field(default_factory=list)
    activity_targets: dict[str, int] = field(default_factory=dict)
    leader_of_flow: str = ""
    release_leaders: str = ""

    # context/jira.md
    jira_projects: list[str] = field(default_factory=list)
    repo_mapping: dict[str, str] = field(default_factory=dict)

    # context/confluence.md
    confluence_root_folder_id: str = ""
    confluence_past_folder_id: str = ""
    confluence_space_key: str = ""

    # PTO calendars (Google Calendar IDs from team.md)
    pto_calendar_ids: list[str] = field(default_factory=list)

    # Main swimlane (fix version name for 360 reports)
    main_swimlane: str = ""

    workspace_path: str = ""


def _parse_table_rows(text: str, header_pattern: str, col_count: int = 0) -> list[list[str]]:
    """Find a markdown table after a line matching header_pattern, return data rows as lists.

    Handles two formats:
      1. Standard markdown tables with | separators
      2. Confluence-stripped tables: alternating non-empty/empty lines, no pipes
         (requires col_count to know how many columns to group)
    """
    lines = text.split("\n")

    # Try standard markdown table first
    rows: list[list[str]] = []
    state = "searching"
    for line in lines:
        stripped = line.strip()
        if state == "searching":
            if re.search(header_pattern, stripped, re.IGNORECASE) and "|" in stripped:
                state = "header_found"
            continue
        if not stripped:
            if state == "data":
                break
            continue
        if "|" not in stripped:
            if state == "data":
                break
            continue
        cells = [c.strip() for c in stripped.split("|")]
        cells = [c for c in cells if c != ""]
        if state == "header_found":
            if all(re.match(r"^[-:]+$", c) for c in cells):
                state = "data"
            continue
        rows.append(cells)
    if rows:
        return rows

    # Fallback: Confluence-stripped format (no pipes, alternating lines)
    if not col_count:
        return []
    values: list[str] = []
    found_header = False
    for line in lines:
        stripped = line.strip()
        if not found_header:
            if re.search(header_pattern, stripped, re.IGNORECASE):
                found_header = True
            continue
        if not stripped:
            continue
        values.append(stripped)

    if len(values) < col_count:
        return []
    # skip the header row (first col_count values are column names)
    data = values[col_count:]
    # stop at first value that looks like a section header (next table or heading)
    cleaned: list[str] = []
    stop_words = {"repositories", "cadence", "activity type", "where work lives"}
    for v in data:
        if v.lower() in stop_words or v.startswith("#"):
            break
        cleaned.append(v)
    return [cleaned[i : i + col_count] for i in range(0, len(cleaned) - col_count + 1, col_count)]


def _extract_board_id(url: str) -> str:
    """Extract board ID from Jira board URL or plain text like 'board 12014'."""
    m = re.search(r"/boards/(\d+)", url)
    if m:
        return m.group(1)
    m = re.search(r"maximized=(\d+)", url)
    if m:
        return m.group(1)
    m = re.search(r"filter[=/](\d+)", url)
    if m:
        return m.group(1)
    m = re.search(r"(?:board|filter)\s+(\d+)", url, re.IGNORECASE)
    if m:
        return m.group(1)
    return ""


def _extract_folder_id(url: str) -> str:
    """Extract Confluence folder/page ID from URL."""
    m = re.search(r"/folder/(\d+)", url)
    if m:
        return m.group(1)
    m = re.search(r"/pages/(\d+)", url)
    if m:
        return m.group(1)
    return ""


def _extract_space_key(url: str) -> str:
    m = re.search(r"/spaces/([A-Z]+)", url)
    return m.group(1) if m else ""


def _field(text: str, name: str) -> str | None:
    """Extract a field value, tolerating both markdown bold and plain text formats.

    Matches: '**Field:** value', 'Field: value', '**Field:** `value`'
    """
    m = re.search(rf"\*\*{name}:\*\*\s*`?([^`\n]+)`?", text)
    if m:
        return m.group(1).strip()
    m = re.search(rf"^{name}:\s*(.+)$", text, re.MULTILINE)
    if m:
        return m.group(1).strip()
    return None


def _extract_calendar_id(raw: str) -> str:
    """Extract Google Calendar ID from a markdown link, Google Calendar URL, or plain text."""
    m = re.search(r"\[([^\]]*)\]\(([^)]+)\)", raw)
    url = m.group(2) if m else raw
    src = re.search(r"[?&]src=([^&\s)]+)", url)
    if src:
        return unquote(src.group(1))
    if "@" in url and not url.startswith("["):
        return url.strip()
    return ""


def _parse_team_md(text: str, config: TeamConfig) -> None:
    config.team_name = _field(text, "Team") or ""
    config.manager = _field(text, "Manager") or ""
    config.charter = _field(text, "Charter") or ""
    config.leader_of_flow = _field(text, "Leader of Flow") or ""
    config.release_leaders = _field(text, "Release Leaders") or ""
    config.jira_label = _field(text, "Jira Label") or ""
    config.main_swimlane = _field(text, "Main Swimlane") or ""

    comp = _field(text, "Jira Components")
    if comp:
        config.jira_components = [re.sub(r"\s*\(.*\)$", "", c.strip()) for c in comp.split(",")]

    # Boards table
    for row in _parse_table_rows(text, r"Board", col_count=2):  # col_count for fallback
        if len(row) >= 2:
            name = row[0]
            url_match = re.search(r"\[([^\]]*)\]\(([^)]+)\)", row[1])
            url = url_match.group(2) if url_match else row[1]
            bid = _extract_board_id(url)
            board = BoardInfo(name=name, board_id=bid, url=url)
            config.boards.append(board)
            if "strats committed" in name.lower():
                config.strats_committed_id = bid
            elif "strats planning" in name.lower():
                config.strats_planning_id = bid

    # Roster table
    for row in _parse_table_rows(text, r"Name", col_count=4):
        if len(row) >= 3:
            raw_email = row[3].strip() if len(row) >= 4 else ""
            em = re.search(r"[\w.+-]+@[\w.-]+\.\w+", raw_email)
            config.roster.append(TeamMember(name=row[0], role=row[1], location=row[2], email=em.group(0) if em else ""))

    # Cadence table
    for row in _parse_table_rows(text, r"Ceremony", col_count=3):
        if len(row) >= 2:
            config.cadence.append({"ceremony": row[0], "frequency": row[1], "notes": row[2] if len(row) > 2 else ""})

    # Activity type targets
    for row in _parse_table_rows(text, r"Activity Type", col_count=2):
        if len(row) >= 2:
            m2 = re.search(r"(\d+)", row[1])
            if m2:
                config.activity_targets[row[0]] = int(m2.group(1))

    # PTO Calendars — extract actual calendar ID from markdown links or URLs
    for row in _parse_table_rows(text, r"Calendar.*ID", col_count=2):
        if len(row) >= 2:
            cal_id = _extract_calendar_id(row[1].strip())
            if cal_id and "@" in cal_id and cal_id not in config.pto_calendar_ids:
                config.pto_calendar_ids.append(cal_id)


def _parse_jira_md(text: str, config: TeamConfig) -> None:
    # Projects
    for row in _parse_table_rows(text, r"Source", col_count=2):
        if len(row) >= 2:
            config.jira_projects.append(row[1])

    # Repo mapping — table format: | Stream | [repo](url) | Purpose |
    for row in _parse_table_rows(text, r"Stream", col_count=3):
        if len(row) >= 2:
            link = re.search(r"\[([^\]]+)\]\(([^)]+)\)", row[1])
            if link:
                config.repo_mapping[row[0].strip()] = link.group(2)


def _parse_confluence_md(text: str, config: TeamConfig) -> None:
    for line in text.split("\n"):
        if "360 root directory" in line.lower():
            m = re.search(r"https?://\S+", line)
            if m:
                url = m.group(0)
                config.confluence_root_folder_id = _extract_folder_id(url)
                config.confluence_space_key = _extract_space_key(url)
        elif "past 360 directory" in line.lower():
            m = re.search(r"https?://\S+", line)
            if m:
                config.confluence_past_folder_id = _extract_folder_id(m.group(0))


def _html_tables_to_md(html: str) -> str:
    """Convert HTML <table> elements to markdown tables, strip everything else."""
    result_parts: list[str] = []
    pos = 0
    for tm in re.finditer(r"<table[^>]*>(.*?)</table>", html, re.DOTALL | re.IGNORECASE):
        # text before the table
        before = html[pos : tm.start()]
        result_parts.append(_strip_html(before))
        # parse table
        table_html = tm.group(1)
        rows_html = re.findall(r"<tr[^>]*>(.*?)</tr>", table_html, re.DOTALL | re.IGNORECASE)
        md_rows: list[list[str]] = []
        for row_html in rows_html:
            cells = re.findall(r"<(?:td|th)[^>]*>(.*?)</(?:td|th)>", row_html, re.DOTALL | re.IGNORECASE)
            md_rows.append([_strip_html(c).strip() for c in cells])
        if md_rows:
            for i, row in enumerate(md_rows):
                result_parts.append("| " + " | ".join(row) + " |")
                if i == 0:
                    result_parts.append("| " + " | ".join("---" for _ in row) + " |")
        result_parts.append("")
        pos = tm.end()
    # text after last table
    result_parts.append(_strip_html(html[pos:]))
    return "\n".join(result_parts)


def _strip_html(text: str) -> str:
    """Strip HTML tags and decode entities."""
    text = re.sub(r"<br\s*/?>", "\n", text)
    text = re.sub(r"</?(?:p|div|li|h[1-6])[^>]*>", "\n", text)
    # extract href from links
    text = re.sub(r'<a[^>]+href="([^"]*)"[^>]*>([^<]*)</a>', r"[\2](\1)", text)
    text = re.sub(r"<[^>]+>", "", text)
    text = re.sub(r"&amp;", "&", text)
    text = re.sub(r"&lt;", "<", text)
    text = re.sub(r"&gt;", ">", text)
    text = re.sub(r"&nbsp;", " ", text)
    text = re.sub(r"&middot;", "·", text)
    text = re.sub(r"&mdash;", "—", text)
    text = re.sub(r"&#\d+;", "", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text


def _fetch_confluence_page(page_id: str) -> str:
    """Fetch a Confluence page's content, converting tables to markdown format."""
    import requests

    base = os.environ.get("CONFLUENCE_URL") or os.environ.get("JIRA_BASE_URL", "").rstrip("/") + "/wiki"
    user = os.environ.get("CONFLUENCE_USERNAME") or os.environ.get("JIRA_EMAIL", "")
    token = os.environ.get("CONFLUENCE_API_TOKEN") or os.environ.get("JIRA_API_TOKEN", "")

    if not all([base, user, token]):
        log.error("Confluence credentials missing — cannot fetch page %s", page_id)
        return ""

    url = f"{base.rstrip('/')}/api/v2/pages/{page_id}?body-format=storage"
    r = requests.get(url, auth=(user, token), timeout=30)
    if not r.ok:
        log.error("Failed to fetch Confluence page %s: HTTP %s", page_id, r.status_code)
        return ""

    body = r.json().get("body", {}).get("storage", {}).get("value", "")
    text = _html_tables_to_md(body)
    log.info("Fetched Confluence page %s (%d chars)", page_id, len(text))
    return text.strip()


def _extract_confluence_page_id(value: str) -> str | None:
    """Extract a Confluence page ID from a full URL or a bare ID.

    Accepts:
        "442272244"
        "https://your-org.atlassian.net/wiki/spaces/TEAM/pages/442272244/Some+Title"
        "https://your-org.atlassian.net/wiki/spaces/TEAM/pages/442272244"
    """
    m = re.search(r"/pages/(\d+)", value)
    if m:
        return m.group(1)
    if value.strip().isdigit():
        return value.strip()
    return None


def _load_sources(ws: Path) -> dict[str, str]:
    """Load source overrides from .team-360-sources.json if it exists.

    File format:
        {
          "team_md": "https://your-org.atlassian.net/wiki/spaces/TEAM/pages/442272244/Team+Overview",
          "jira_md": "https://your-org.atlassian.net/wiki/spaces/TEAM/pages/123456789",
          "confluence_md": "local"
        }

    Values:
        "local"                     — read from disk (default behavior)
        "<confluence-url>"          — full Confluence page URL
        "<page-id>"                 — bare numeric page ID
    """
    sources_file = ws / ".team-360-sources.json"
    if not sources_file.exists():
        return {}
    try:
        return json.loads(sources_file.read_text())
    except (json.JSONDecodeError, OSError) as e:
        log.warning("Failed to parse %s: %s", sources_file, e)
        return {}


def _resolve_source(key: str, local_path: Path, sources: dict[str, str]) -> str:
    """Return file content from either local disk or Confluence, based on sources config."""
    override = sources.get(key, "local")
    if override == "local":
        if local_path.exists():
            return local_path.read_text()
        return ""
    page_id = _extract_confluence_page_id(override)
    if page_id:
        log.info("Loading %s from Confluence page %s", key, page_id)
        return _fetch_confluence_page(page_id)
    log.warning("Cannot parse Confluence page ID from %s value: %s", key, override)
    return ""


def load_config(workspace: str | None = None, sources: dict[str, str] | None = None) -> TeamConfig:
    """Load team configuration from workspace markdown files or Confluence pages.

    Args:
        workspace: path to workspace root (contains team.md, context/)
        sources: optional dict mapping file keys to Confluence URLs or "local".
                 If provided, takes precedence over .team-360-sources.json.
    """
    ws = Path(workspace) if workspace else Path.cwd()
    config = TeamConfig(workspace_path=str(ws))
    if sources is None:
        sources = _load_sources(ws)

    team_text = _resolve_source("team_md", ws / "team.md", sources)
    if team_text:
        _parse_team_md(team_text, config)
        log.debug("Loaded team.md (source: %s)", sources.get("team_md", "local"))
    else:
        log.warning("team.md not found (checked local and sources override)")

    jira_text = _resolve_source("jira_md", ws / "context" / "jira.md", sources)
    if jira_text:
        _parse_jira_md(jira_text, config)
        log.debug("Loaded jira.md (source: %s)", sources.get("jira_md", "local"))

    conf_text = _resolve_source("confluence_md", ws / "context" / "confluence.md", sources)
    if conf_text:
        _parse_confluence_md(conf_text, config)
        log.debug("Loaded confluence.md (source: %s)", sources.get("confluence_md", "local"))

    # Override jira_projects if explicitly set in sources (e.g. from teams.json)
    if sources.get("jira_projects"):
        config.jira_projects = [p.strip() for p in sources["jira_projects"].split(",")]
        log.debug("jira_projects overridden from sources: %s", config.jira_projects)

    return config
