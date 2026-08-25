"""Confluence publisher — Basic Auth. Page creation uses API v2 (accepts folders as parents)."""

from __future__ import annotations

import logging
import os
from datetime import datetime, timezone

import markdown
import requests

log = logging.getLogger(__name__)


def _v2_base(confluence_url: str) -> str:
    """Build the Confluence Cloud API v2 base URL, tolerating a URL with or without /wiki.

    v1 usage was `{url}/rest/api/content`, which on Cloud lives under /wiki — so
    confluence_url usually already includes /wiki. Don't double it.
    """
    base = confluence_url.rstrip("/")
    return f"{base}/api/v2" if base.endswith("/wiki") else f"{base}/wiki/api/v2"


def _resolve_space_id(confluence_auth: tuple, v2_base: str, space_key: str) -> str:
    """Resolve the numeric spaceId required by API v2 from the space key."""
    r = requests.get(f"{v2_base}/spaces", auth=confluence_auth, params={"keys": space_key}, timeout=30)
    r.raise_for_status()
    results = r.json().get("results", [])
    if not results:
        raise ValueError(f"No Confluence space found for key {space_key!r}")
    return str(results[0]["id"])


def fetch_previous_360(confluence_auth: tuple, confluence_url: str, root_dir_id: int) -> tuple[dict | None, str | None]:
    """Find most recent 360 page under root dir. Returns (page_data, page_url)."""
    url = f"{confluence_url.rstrip('/')}/rest/api/content/{root_dir_id}/child/page"
    try:
        r = requests.get(url, auth=confluence_auth, params={"limit": 25, "expand": "version"}, timeout=30)
        r.raise_for_status()
        pages = [
            p
            for p in r.json().get("results", [])
            if "360" in p.get("title", "") and "-test" not in p.get("title", "").lower()
        ]
        if pages:
            page = pages[0]
            webui = page.get("_links", {}).get("webui", "")
            base = confluence_url.rstrip("/")
            page_url = f"{base}{webui}" if webui else f"{base}/pages/{page['id']}"
            return page, page_url
    except Exception as e:
        log.warning("Failed to fetch previous 360: %s", e)
    return None, None


def archive_previous_360(confluence_auth: tuple, confluence_url: str, page_id: int, past_dir_id: int) -> bool:
    """Move current 360 page to Past directory. Returns success."""
    try:
        base = confluence_url.rstrip("/")
        r = requests.get(
            f"{base}/rest/api/content/{page_id}",
            auth=confluence_auth,
            params={"expand": "version,body.storage"},
            timeout=30,
        )
        r.raise_for_status()
        page = r.json()

        r = requests.put(
            f"{base}/rest/api/content/{page_id}",
            auth=confluence_auth,
            timeout=30,
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


def publish_360(
    confluence_auth: tuple, confluence_url: str, root_dir_id: int, title: str, html_content: str, space_key: str = ""
) -> tuple[int, str]:
    """Create new 360 page via Confluence API v2. Returns (page_id, page_url).

    v2 accepts a folder as parentId (v1 /rest/api/content rejects folders as ancestors -> 400).
    """
    if not space_key:
        raise ValueError("space_key is required to resolve the numeric spaceId for Confluence API v2")

    v2_base = _v2_base(confluence_url)
    space_id = _resolve_space_id(confluence_auth, v2_base, space_key)
    body = {
        "spaceId": space_id,
        "status": "current",
        "title": title,
        "parentId": str(root_dir_id),
        "body": {"representation": "storage", "value": html_content},
    }

    try:
        r = requests.post(f"{v2_base}/pages", auth=confluence_auth, json=body, timeout=60)
        r.raise_for_status()
    except requests.HTTPError as e:
        if e.response is not None:
            log.error("Confluence v2 create failed (%s): %s", e.response.status_code, e.response.text)
        raise

    result = r.json()
    page_id = int(result["id"])
    links = result.get("_links", {})
    webui = links.get("webui", "")
    link_base = links.get("base", "")
    if webui:
        page_url = f"{link_base}{webui}" if link_base else f"{confluence_url.rstrip('/')}{webui}"
    else:
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
