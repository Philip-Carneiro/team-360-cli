"""Confluence publisher — Basic Auth. Page creation uses API v2 (accepts folders as parents)."""

from __future__ import annotations

import logging

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
    """Find most recent 360 page under root dir (API v2). Returns (page_data, page_url)."""
    v2_base = _v2_base(confluence_url)
    try:
        r = requests.get(
            f"{v2_base}/folders/{root_dir_id}/children",
            auth=confluence_auth,
            params={"limit": 25},
            timeout=30,
        )
        r.raise_for_status()
        pages = [
            p
            for p in r.json().get("results", [])
            if "360" in p.get("title", "") and "-test" not in p.get("title", "").lower()
        ]
        if pages:
            page = pages[0]
            page_id = page["id"]
            webui = page.get("_links", {}).get("webui", "")
            base = confluence_url.rstrip("/")
            page_url = f"{base}{webui}" if webui else f"{base}/pages/{page_id}"
            # Preserve contract: main.py reads prev_data["title"] and prev_data["id"]
            return {"id": page_id, "title": page.get("title", "")}, page_url
    except Exception as e:
        log.warning("Failed to fetch previous 360: %s", e)
    return None, None


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
