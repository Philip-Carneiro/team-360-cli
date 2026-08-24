"""Obsidian vault publisher."""

from __future__ import annotations

import logging
import os
from pathlib import Path

log = logging.getLogger(__name__)


def save_to_vault(report: str, team_name: str, date_str: str, test_mode: bool, emoji: str = "") -> str | None:
    """Save report to $OBSIDIAN_VAULT/SCRUMBAN/STATUS/360/. Returns path or None."""
    vault = os.environ.get("OBSIDIAN_VAULT")
    if not vault:
        log.info("OBSIDIAN_VAULT not set, skipping vault save")
        return None

    target_dir = Path(vault) / "SCRUMBAN" / "STATUS" / "360"
    target_dir.mkdir(parents=True, exist_ok=True)

    suffix = "-test" if test_mode else ""
    prefix = f"{emoji} " if emoji else ""
    filename = f"{prefix}{team_name} 360 - {date_str}{suffix}.md"
    path = target_dir / filename
    path.write_text(report, encoding="utf-8")
    log.info("Saved to vault: %s", path)
    return str(path)
