"""Confluence publisher — REST API v1 with Basic Auth."""

from __future__ import annotations

import logging
import os
from datetime import datetime, timezone

import markdown
import requests

log = logging.getLogger(__name__)


def fetch_previous_360(confluence_auth: tuple, confluence_url: str,
                       root_dir_id: int) -> tuple[dict | None, str | None]:
    """Find most recent 360 page under root dir. Returns (page_data, page_url)."""
    url = f"{confluence_url.rstrip('/')}/rest/api/content/{root_dir_id}/child/page"
    try:
        r = requests.get(url, auth=confluence_auth,
                         params={"limit": 25, "expand": "version"},
                         timeout=30)
        r.raise_for_status()
        pages = [p for p in r.json().get("results", [])
                 if "360" in p.get("title", "") and "-test" not in p.get("title", "").lower()]
        if pages:
            page = pages[0]
            page_url = f"{confluence_url.rstrip('/')}/pages/{page['id']}"
            return page, page_url
    except Exception as e:
        log.warning("Failed to fetch previous 360: %s", e)
    return None, None


def archive_previous_360(confluence_auth: tuple, confluence_url: str,
                         page_id: int, past_dir_id: int) -> bool:
    """Move current 360 page to Past directory. Returns success."""
    try:
        base = confluence_url.rstrip("/")
        r = requests.get(f"{base}/rest/api/content/{page_id}",
                         auth=confluence_auth,
                         params={"expand": "version,body.storage"},
                         timeout=30)
        r.raise_for_status()
        page = r.json()

        r = requests.put(
            f"{base}/rest/api/content/{page_id}",
            auth=confluence_auth, timeout=30,
            json={
                "type": "page",
                "title": page["title"],
                "version": {"number": page["version"]["number"] + 1},
                "ancestors": [{"id": str(past_dir_id)}],
                "body": page.get("body", {}),
            },
        )
        r.raise_for_status()
        log.info("Archived page %s to past directory %s", page_id, past_dir_id)
        return True
    except Exception as e:
        log.error("Failed to archive page %s: %s", page_id, e)
        return False


def publish_360(confluence_auth: tuple, confluence_url: str,
                root_dir_id: int, title: str, html_content: str,
                space_key: str = "") -> tuple[int, str]:
    """Create new 360 page. Returns (page_id, page_url)."""
    body: dict = {
        "type": "page",
        "title": title,
        "ancestors": [{"id": str(root_dir_id)}],
        "body": {"storage": {"value": html_content, "representation": "storage"}},
    }
    if space_key:
        body["space"] = {"key": space_key}

    r = requests.post(f"{confluence_url.rstrip('/')}/rest/api/content",
                      auth=confluence_auth, json=body, timeout=60)
    r.raise_for_status()
    result = r.json()
    page_id = int(result["id"])
    page_url = f"{confluence_url.rstrip('/')}/pages/{page_id}"
    log.info("Published page %s: %s", page_id, page_url)
    return page_id, page_url


def _md_to_html(md_text: str) -> str:
    return markdown.markdown(md_text, extensions=["tables", "fenced_code"])


def publish(report_md: str, config: dict, test_mode: bool) -> str | None:
    """Full publish workflow: fetch previous, archive (production), create new. Returns URL."""
    conf_url = config.get("confluence_url") or os.environ.get("CONFLUENCE_URL", "")
    username = config.get("confluence_username") or os.environ.get("CONFLUENCE_USERNAME", "")
    token = config.get("confluence_api_token") or os.environ.get("CONFLUENCE_API_TOKEN", "")
    root_id = config.get("confluence_root_dir_id", "")
    past_id = config.get("confluence_past_dir_id", "")
    space_key = config.get("confluence_space_key", "")
    team = config.get("team_name", "Team")

    if not all([conf_url, username, token, root_id]):
        log.info("Confluence not fully configured, skipping publish")
        return None

    auth = (username, token)
    root_dir_id = int(root_id)
    date_str = datetime.now(timezone.utc).strftime("%d/%m/%Y")
    prefix = "test-" if test_mode else ""
    title = f"{prefix}{team} 360 - {date_str}"

    if not test_mode and past_id:
        prev_page, _ = fetch_previous_360(auth, conf_url, root_dir_id)
        if prev_page:
            archive_previous_360(auth, conf_url, int(prev_page["id"]), int(past_id))

    html = _md_to_html(report_md)
    _, page_url = publish_360(auth, conf_url, root_dir_id, title, html, space_key)
    return page_url
