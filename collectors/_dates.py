"""Shared date helpers for collectors."""

from __future__ import annotations

from datetime import datetime, timezone


def _parse_iso(s: str | None) -> datetime | None:
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None


def _days_since(dt: datetime | None) -> int | None:
    if not dt:
        return None
    return max((datetime.now(timezone.utc) - dt).days, 0)
