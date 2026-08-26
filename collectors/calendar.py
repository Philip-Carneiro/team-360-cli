"""Google Calendar absence collector — fetches PTO, OOO, and sick day events."""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import quote

import requests

log = logging.getLogger(__name__)

TOKEN_FILE = Path.home() / ".config" / "google-calendar-token.json"

_PTO_KEYWORDS = {"pto", "ooo", "leave", "vacation", "holiday", "off", "out of office", "annual leave"}
_SICK_KEYWORDS = {"sick", "sickday", "sick day", "sick leave", "medical", "unwell", "ill"}
_SKIP_EXACT = {"ooo", "pto", "office hours", "meeting", "standup", "sync", "1:1", "team", "sick", "sickday", "sick day"}


def _load_google_creds() -> tuple[str, str, str] | None:
    """Load Google OAuth creds from env vars, falling back to token file."""
    client_id = os.environ.get("GOOGLE_CLIENT_ID", "")
    client_secret = os.environ.get("GOOGLE_CLIENT_SECRET", "")
    refresh_token = os.environ.get("GOOGLE_REFRESH_TOKEN", "")

    if all([client_id, client_secret, refresh_token]):
        return client_id, client_secret, refresh_token

    if not TOKEN_FILE.exists():
        log.warning(
            "Google Calendar: no env vars (GOOGLE_CLIENT_ID, GOOGLE_CLIENT_SECRET, "
            "GOOGLE_REFRESH_TOKEN) and no token file at %s",
            TOKEN_FILE,
        )
        return None
    try:
        creds = json.loads(TOKEN_FILE.read_text())
    except (json.JSONDecodeError, OSError) as e:
        log.warning("Failed to read token file: %s", e)
        return None

    client_id = creds.get("client_id", "")
    client_secret = creds.get("client_secret", "")
    refresh_token = creds.get("refresh_token", "")

    if not all([client_id, client_secret, refresh_token]):
        log.warning("Token file missing required fields")
        return None
    return client_id, client_secret, refresh_token


def _refresh_token() -> str | None:
    creds = _load_google_creds()
    if not creds:
        return None
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
        return r.json().get("access_token")
    except Exception as e:
        log.warning("Token refresh failed: %s", e)
        return None


def _fetch_events(token: str, calendar_id: str, time_min: str, time_max: str) -> list[dict]:
    encoded_id = quote(calendar_id, safe="")
    url = f"https://www.googleapis.com/calendar/v3/calendars/{encoded_id}/events"
    params = {
        "timeMin": time_min,
        "timeMax": time_max,
        "singleEvents": "true",
        "orderBy": "startTime",
        "maxResults": 250,
    }
    try:
        r = requests.get(url, headers={"Authorization": f"Bearer {token}"}, params=params, timeout=30)
        r.raise_for_status()
        return r.json().get("items", [])
    except Exception as e:
        log.warning("Failed to fetch calendar %s: %s", calendar_id[:30], e)
        return []


def _classify_absence(summary: str) -> str:
    """Classify absence type from event title. Returns 'sick', 'pto', or 'pto' as default."""
    low = summary.lower()
    for kw in _SICK_KEYWORDS:
        if kw in low:
            return "sick"
    return "pto"


def _match_event_to_roster(summary: str, roster: list[dict]) -> str | None:
    if not summary:
        return None
    low = summary.lower().strip()
    # skip generic titles without a person's name
    if low in _SKIP_EXACT:
        return None
    for member in roster:
        name = member["name"]
        parts = name.lower().split()
        if name.lower() in low:
            return name
        if parts and len(parts[0]) >= 3 and parts[0] in low:
            return name
        email = member.get("email", "")
        if email:
            username = email.split("@")[0].lower()
            if len(username) >= 3 and username in low:
                return name
    return None


def _parse_event_dates(event: dict) -> list[tuple[str, datetime]]:
    """Parse event dates, returning (display_str, sort_key_datetime) tuples."""
    start = event.get("start", {})
    end = event.get("end", {})

    if "date" in start:
        s = datetime.strptime(start["date"], "%Y-%m-%d")
        e = datetime.strptime(end.get("date", start["date"]), "%Y-%m-%d")
        dates = []
        d = s
        while d < e:
            dates.append((d.strftime("%d/%m"), d))
            d += timedelta(days=1)
        return dates or [(s.strftime("%d/%m"), s)]
    elif "dateTime" in start:
        s = datetime.fromisoformat(start["dateTime"])
        return [(s.strftime("%d/%m"), s)]
    return []


def collect_absences(
    calendar_ids: list[str], roster: list[dict], since_date: str, until_date: str | None = None
) -> dict[str, dict[str, list[str]]]:
    """Collect absences from Google Calendar, classified by type.

    Returns:
        {"Person": {"pto": ["DD/MM", ...], "sick": ["DD/MM", ...]}}
    """
    if not calendar_ids:
        log.info("No PTO calendars configured, skipping")
        return {}

    token = _refresh_token()
    if not token:
        log.warning("Google Calendar auth unavailable")
        return {}

    time_min = f"{since_date}T00:00:00Z"
    time_max = f"{until_date}T23:59:59Z" if until_date else datetime.now(timezone.utc).strftime("%Y-%m-%dT23:59:59Z")

    raw: dict[str, dict[str, list[tuple[str, datetime]]]] = {}

    for cal_id in calendar_ids:
        events = _fetch_events(token, cal_id, time_min, time_max)
        log.info("Calendar %s: %d events", cal_id[:20], len(events))
        for event in events:
            summary = event.get("summary", "")
            matched = _match_event_to_roster(summary, roster)
            if matched:
                absence_type = _classify_absence(summary)
                dates = _parse_event_dates(event)
                raw.setdefault(matched, {}).setdefault(absence_type, []).extend(dates)

    result: dict[str, dict[str, list[str]]] = {}
    for name, types in raw.items():
        result[name] = {}
        for atype, date_tuples in types.items():
            sorted_tuples = sorted(date_tuples, key=lambda x: x[1])
            seen: set[str] = set()
            dedupe = []
            for display, _ in sorted_tuples:
                if display not in seen:
                    seen.add(display)
                    dedupe.append(display)
            result[name][atype] = dedupe

    if result:
        log.info("Absences detected: %s", {k: {t: len(d) for t, d in v.items()} for k, v in result.items()})
    return result
