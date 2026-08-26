"""Report generator — produces full markdown 360 report from analyzed data."""

from __future__ import annotations

import logging
import random
import re
from datetime import datetime, timezone

from heuristics import _jira_base, _stale_level

_TITLE_EMOJIS = ["🟢", "🚀", "🔥", "⚡", "🎯", "🏁", "📊", "🌟", "💎", "🛡️", "🎲", "🧭", "🔮", "🎪", "🏆"]

log = logging.getLogger(__name__)


def _get_name(ticket: dict, field: str, default: str = "") -> str:
    raw = ticket.get(field, default)
    if isinstance(raw, dict):
        return raw.get("displayName", raw.get("name", default))
    return str(raw) if raw else default


def _jl(key: str) -> str:
    base = _jira_base()
    return f"[{key}]({base}/browse/{key})" if base else key


def _linkify_ticket_keys(md: str) -> str:
    base = _jira_base()
    if not base:
        return md
    return re.sub(
        r"(?<![\[/\w-])([A-Z][A-Z0-9]+-\d+)(?!\]|\))",
        rf"[\1]({base}/browse/\1)",
        md,
    )


def _md_table(headers: list[str], rows: list[list[str]]) -> str:
    if not rows:
        return ""
    sep = ["---"] * len(headers)
    lines = ["| " + " | ".join(headers) + " |", "| " + " | ".join(sep) + " |"]
    for row in rows:
        padded = list(row) + [""] * (len(headers) - len(row))
        lines.append("| " + " | ".join(str(c) for c in padded) + " |")
    return "\n".join(lines)


def _classify(t: dict, roster: list[dict]) -> str:
    assignee = _get_name(t, "assignee")
    if not assignee:
        return "UNASSIGNED"
    low = assignee.lower()
    for m in roster:
        mlow = m["name"].lower()
        if low in mlow or mlow in low:
            return "ROSTER"
    return "THIRD-PARTY"


def _pr_icon(state: str) -> str:
    return {"MERGED": "✅", "OPEN": "⏳", "CLOSED": "❌"}.get(state, "?")


def _pr_cell(pr_links: list[dict]) -> str:
    if not pr_links:
        return "—"
    seen: set[str] = set()
    parts = []
    for p in pr_links:
        url = p.get("url", "")
        key = url or id(p)
        if key in seen:
            continue
        seen.add(key)
        plat = "GH" if p.get("platform") == "github" else "GL"
        num = url.rstrip("/").split("/")[-1] if url else "?"
        prefix = "#" if plat == "GH" else "!"
        label = f"{plat}:{prefix}{num} {_pr_icon(p.get('state', ''))}"
        parts.append(f"[{label}]({url})" if url else label)
    return " / ".join(parts)


def _pr_open_days(pr_links: list[dict]) -> str:
    ages = [p["age_days"] for p in pr_links if p.get("age_days") is not None and p.get("state") == "OPEN"]
    return str(max(ages)) if ages else "—"


def _pr_link_rich(p: dict) -> str:
    url = p.get("url", "")
    num = url.rstrip("/").split("/")[-1] if url else "?"
    short = ""
    if url and "github.com" in url:
        parts = url.split("github.com/")[-1].split("/pull/")[0].split("/")
        short = parts[-1] if parts else ""
    elif url and "merge_requests" in url:
        parts = url.split("/-/merge_requests/")[0].split("/")
        short = parts[-1] if parts else ""
    icon = _pr_icon(p.get("state", ""))
    prefix = "#" if p.get("platform", "github") == "github" else "!"
    label = f"{short}{prefix}{num}" if short else f"{prefix}{num}"
    return f"[{label}]({url}) {icon}" if url else f"{label} {icon}"


def _pr_blocker(t: dict) -> str:
    pr_links = t.get("pr_links", [])
    if not pr_links:
        if "spike" in (t.get("summary") or "").lower():
            return "Spike — no findings doc linked"
        return "No PR links"
    parts = []
    for p in pr_links:
        health = p.get("pr_health", "")
        detail = p.get("pr_health_detail", "")
        if health == "BUILD_FAILING":
            parts.append(f"\U0001f534 {detail or 'Build failing'}")
        elif health == "CHANGES_REQUESTED_NOT_ADDRESSED":
            parts.append(f"⚠️ {detail or 'CR not addressed'}")
        elif health == "CHANGES_ADDRESSED_WAITING_REREVIEW":
            parts.append(f"⏳ {detail or 'Awaiting re-review'}")
        elif health == "AWAITING_INITIAL_REVIEW":
            parts.append(f"⏳ {detail or 'Awaiting review'}")
        elif health == "APPROVED":
            parts.append("✅ Approved, ready to merge")
        elif health == "BUILD_FLAKY":
            parts.append(f"\U0001f7e1 {detail or 'Build may be flaky'}")
    composite = t.get("composite_pr_status", "")
    status = (t.get("status") or "").lower()
    if composite == "PARTIALLY_MERGED":
        for p in pr_links:
            if p.get("state") == "OPEN":
                parts.append(f"{p.get('platform', 'unknown')} PR still open")
    if composite == "ALL_MERGED" and status in ("review", "in review"):
        parts.append("✅ All merged — transition to Testing")
    return " / ".join(parts) if parts else "— Active"


def _delta_str(current: int, previous: int | None) -> str:
    if previous is None:
        return "—"
    if current > previous:
        return f"up from {previous}"
    if current < previous:
        return f"down from {previous}"
    return f"= {previous}"


# --- Previous report parsers ---


def _parse_prev_snapshot(text: str) -> dict[str, int]:
    result: dict[str, int] = {}
    in_section = False
    for line in text.split("\n"):
        if line.startswith("## ") and "Snapshot Stats" in line:
            in_section = True
            continue
        if in_section and line.startswith("## "):
            break
        if in_section and "|" in line:
            cells = [c.strip() for c in line.split("|")]
            cells = [c for c in cells if c and not all(ch in "-:" for ch in c)]
            if len(cells) >= 2:
                metric = cells[0].replace("**", "").strip()
                m = re.search(r"\d+", cells[1].replace("**", ""))
                if m and metric:
                    result[metric] = int(m.group())
    return result


def _parse_prev_activity(text: str) -> dict[str, int]:
    result: dict[str, int] = {}
    in_section = False
    for line in text.split("\n"):
        if line.startswith("## ") and "Release Progress" in line:
            in_section = True
            continue
        if in_section and line.startswith("## "):
            break
        if in_section and "|" in line:
            cells = [c.strip() for c in line.split("|")]
            cells = [c for c in cells if c and not all(ch in "-:" for ch in c)]
            if len(cells) >= 3:
                name = cells[0].strip()
                pct = re.search(r"(\d+)%", cells[2])
                if pct and name not in ("Activity Type", ""):
                    result[name] = int(pct.group(1))
    return result


def _parse_prev_epics(text: str) -> dict[str, int]:
    result: dict[str, int] = {}
    current_key = None
    for line in text.split("\n"):
        m = re.search(r"###\s+\[([A-Z]+-\d+)\]", line)
        if m:
            current_key = m.group(1)
        if current_key and "Completion:" in line:
            pct = re.search(r"(\d+)%", line)
            if pct:
                result[current_key] = int(pct.group(1))
                current_key = None
    return result


def _parse_prev_stale_keys(text: str) -> set[str]:
    result: set[str] = set()
    state = "searching"
    for line in text.split("\n"):
        if state == "searching":
            if line.startswith("## ") and "Risk" in line:
                state = "in_section"
            continue
        if state == "in_section":
            if line.startswith("## ") and "4." not in line:
                break
            if "|" in line and re.search(r"\[[A-Z]+-\d+\]", line):
                state = "in_table"
                m = re.search(r"\[([A-Z]+-\d+)\]", line)
                if m:
                    result.add(m.group(1))
            continue
        if state == "in_table":
            if "|" in line:
                m = re.search(r"\[([A-Z]+-\d+)\]", line)
                if m:
                    result.add(m.group(1))
            else:
                break
    return result


def _parse_prev_strats(text: str) -> dict[str, str]:
    """Parse STRAT key -> status from previous report's STRAT table."""
    result: dict[str, str] = {}
    in_section = False
    for line in text.split("\n"):
        if line.startswith("## ") and "STRAT" in line:
            in_section = True
            continue
        if in_section and line.startswith("## "):
            break
        if in_section and "|" in line:
            m = re.search(r"\[([A-Z]+-\d+)\]", line)
            if m:
                cells = [c.strip() for c in line.split("|")]
                cells = [c for c in cells if c and not all(ch in "-:" for ch in c)]
                # cells[0]=Feature, cells[1]=Status
                if len(cells) >= 2:
                    result[m.group(1)] = cells[1].strip()
    return result


def _clean_status_summary(text: str) -> str:
    if not text:
        return "—"
    text = re.sub(r"^(RHAI\s+\w+\s+Team\s*[-–—]?\s*)", "", text)
    text = re.sub(
        r"(Dashboard\s*[-–—]\s*\w+\s*[-–—]\s*)?Status\s*[-–—]?\s*(Green|Yellow|Red|Orange)\s*\U0001f7e2?\s*[-–—]?\s*",
        "",
        text,
        flags=re.IGNORECASE,
    )
    text = re.sub(r"Target\s+\d+\.\d+\s*(Stable|EA|GA|TP|DP)?\s*", "", text, flags=re.IGNORECASE)
    text = re.sub(r"^\s*[-–—]\s*", "", text).strip()
    text = re.sub(r"([a-z])\.([A-Z])", r"\1. \2", text)
    text = re.sub(r"([a-z])([A-Z])", r"\1 \2", text)
    text = re.sub(r"(teams)(we|they|it|he|she)", r"\1 \2", text)
    text = re.sub(r"(most)(\w)", r"\1 \2", text, count=1)
    text = re.sub(r"\s{2,}", " ", text).strip()
    if not text:
        return "—"

    return text


# --- Section builders ---


def _fmt_prev_link(url: str | None, title: str | None) -> str:
    if not url:
        return "N/A"
    if title:
        return f"[{title}]({url})"
    return url


def _header(
    config: dict,
    test_mode: bool,
    prev_url: str | None,
    ctime: str,
    prev_title: str | None = None,
    trend: str = "",
    emoji: str = "",
) -> str:
    now = datetime.now(timezone.utc)
    team = config.get("team_name", "Team")
    gen_time = now.strftime("%Y-%m-%d") + " at " + now.strftime("%H:%M") + " UTC"

    board_link = ""
    boards = config.get("boards", {})
    if isinstance(boards, dict):
        for name, info in boards.items():
            if "doing" in name.lower() or "scrumban" in name.lower():
                url = info.get("url", "") if isinstance(info, dict) else str(info)
                board_link = f"[{name}]({url})"
                break
    if not board_link:
        board_link = config.get("doing_board_name", "Board")
        if config.get("doing_board_url"):
            board_link = f"[{board_link}]({config['doing_board_url']})"

    title_emoji = emoji or random.choice(_TITLE_EMOJIS)
    lines = [f"# {title_emoji} {team} 360 — {now.strftime('%A %d %B %Y')}", ""]
    if test_mode:
        lines += ["> **TEST MODE** — this page was generated as a dry run. Do not use for ceremony decisions.", ""]
    fields = [
        f"**Generated**: {gen_time}",
        f"**Board**: {board_link or 'N/A'}",
        "**Methodology**: Scrumban",
    ]
    if config.get("leader_of_flow"):
        fields.append(f"**Leader of Flow**: {config['leader_of_flow']}")
    if config.get("release_leaders"):
        fields.append(f"**Release Leaders**: {config['release_leaders']}")
    fields += [
        "**Generated by**: team-360-cli",
        f"**Prerequisite**: JIRA + GitHub data collected at {ctime}",
        f"**Previous 360**: {_fmt_prev_link(prev_url, prev_title)}",
        f"**Trend**: {trend or 'No comparison baseline.'}",
    ]
    if config.get("swimlane_filter"):
        label = "Swimlanes" if "," in config["swimlane_filter"] else "Swimlane"
        fields.append(f"**{label}**: {config['swimlane_filter']}")
    # Use pipe-table style to keep each field on its own rendered line.
    lines += ["\n".join(fields), "", "---", ""]
    return "\n".join(lines)


def _s_wins(a: dict) -> str:
    wins = a.get("wins", [])
    if not wins:
        return ""
    lines = ["## 1. Wins", ""]
    rows = []
    for w in wins:
        who = w.get("who", "Team")
        if isinstance(who, dict):
            who = who.get("displayName", "Team")
        rows.append([w.get("win", ""), str(who)])
    lines.append(_md_table(["Win", "Who"], rows))
    lines += ["", "---", ""]
    return "\n".join(lines)


def _s_agenda(a: dict) -> str:
    board_map = {t["key"]: t for t in a.get("doing_board", [])}
    lines = ["## 2. Agenda", "", "Items requiring team discussion, ordered by priority:", ""]

    positives = []
    epics = a.get("epic_progress", [])
    completed = a.get("completed", [])
    near_complete = [
        e for e in epics if e.get("total_children", 0) > 0 and e.get("done_count", 0) / e["total_children"] >= 0.7
    ]
    if near_complete or len(completed) >= 3:
        parts = []
        if near_complete:
            pcts = ", ".join(
                f"{e.get('summary', '')} {round(e['done_count'] / e['total_children'] * 100)}%"
                for e in near_complete[:3]
            )
            parts.append(f"{len(near_complete)} Epics nearing completion ({pcts})")
        if completed:
            parts.append(f"{len(completed)} tickets completed this cycle")
        positives.append(f"**Positive momentum.** {'; '.join(parts)}. (Positive signal)")

    ticket_items = []
    heuristic_agenda = a.get("agenda", [])
    overloaded_names = {o["name"] for o in a.get("overloaded_engineers", [])}
    for item in heuristic_agenda:
        text = item["text"]
        key_match = re.search(r"\[([A-Z]+-\d+)\]", text)
        if key_match:
            key = key_match.group(1)
            ticket = board_map.get(key)
            if ticket:
                ticket_items.append(_enrich_agenda_text(key, ticket))
                continue
        if any(name in text for name in overloaded_names):
            continue
        ticket_items.append(text)
    ticket_items = ticket_items[:4]

    concerns = []
    for o in a.get("overloaded_engineers", [])[:1]:
        sigs = ", ".join(o["signals"][:2])
        concerns.append(f"**{o['name']} overload** — {o['item_count']} items: {sigs}.")

    all_items = positives + ticket_items + concerns
    all_items = all_items[:6]

    if not all_items:
        lines.append("No agenda items this week.")
    else:
        for i, item in enumerate(all_items, 1):
            lines.append(f"{i}. {item}")

    lines += ["", "---", ""]
    return "\n".join(lines)


def _enrich_agenda_text(key: str, ticket: dict) -> str:
    summary = ticket.get("summary", "")
    status = _get_name(ticket, "status")
    days = ticket.get("days_in_status", 0)
    assignee = _get_name(ticket, "assignee", "Unassigned")
    pr_links = ticket.get("pr_links", [])
    composite = ticket.get("composite_pr_status", "")

    parts = [f'{_jl(key)} — "{summary}" in {status} **{days} days** ({assignee}).']

    if pr_links:
        pr_strs = [_pr_link_rich(p) for p in pr_links]
        if composite == "PARTIALLY_MERGED":
            parts.append(f"**PARTIALLY_MERGED**: {' / '.join(pr_strs)}.")
        else:
            parts.append(f"PR {' / '.join(pr_strs)}.")
        for p in pr_links:
            h = p.get("pr_health", "")
            if h == "CHANGES_REQUESTED_NOT_ADDRESSED":
                parts.append("Changes requested, not addressed.")
                break
            if h == "BUILD_FAILING":
                parts.append("Build failing.")
                break
    elif status.lower() in ("review", "in review"):
        parts.append("No PR links on ticket.")

    if not pr_links and status.lower() in ("review", "in review"):
        parts.append("Action: determine status.")
    elif any(p.get("pr_health") == "CHANGES_REQUESTED_NOT_ADDRESSED" for p in pr_links):
        parts.append("Action: address CR or close.")
    elif assignee == "Unassigned":
        parts.append("Action: assign owner.")
    elif days >= 60:
        parts.append("Action: close, reassign, or time-box.")

    if days >= 21:
        approx = days // 7
        months = days // 30
        time_str = f"~{months} months" if months >= 2 else f"~{approx} weeks"
        parts.append(f"(Carried over — ~{approx} cycles / {time_str})")
    elif days >= 7:
        parts.append("(New at this level)")
    else:
        parts.append("(New)")

    return " ".join(parts)


def _s_snapshot(a: dict, prev_snapshot: dict[str, int] | None = None) -> str:
    s = a.get("snapshot_stats", {})
    has_delta = bool(prev_snapshot)

    headers = ["Metric", "Count"]
    if has_delta:
        headers.append("Delta vs previous")

    def _row(metric: str, value: int) -> list[str]:
        r = [metric, str(value)]
        if has_delta:
            clean = metric.replace("**", "").strip()
            r.append(_delta_str(value, prev_snapshot.get(clean)))
        return r

    rows = [
        _row("In Progress (roster + unassigned)", s.get("in_progress", 0)),
        _row("In Review (roster + unassigned)", s.get("in_review", 0)),
        _row("Testing (roster + unassigned)", s.get("testing", 0)),
    ]
    total = s.get("total_doing", 0)
    total_row = ["**Total doing (roster + unassigned)**", f"**{total}**"]
    if has_delta:
        prev_total = prev_snapshot.get("Total doing (roster + unassigned)")
        total_row.append(f"**{_delta_str(total, prev_total)}**" if prev_total is not None else "—")
    rows.append(total_row)
    rows += [
        _row("↳ of which Unassigned", s.get("unassigned", 0)),
        _row("Stale (7+ days, non-Epic, non-Testing)", s.get("stale", 0)),
        _row("Critical Bugs (open, roster)", s.get("critical_bugs", 0)),
        _row("Completed since last 360", s.get("completed", 0)),
        _row("Active engineers (roster)", s.get("active_engineers", 0)),
        _row("Third-party assigned (separate section)", s.get("third_party", 0)),
    ]

    parts = [
        "## 3. Snapshot Stats",
        "",
        "> Counts below include **only roster members and unassigned tickets**. "
        "Third-party assigned tickets are reported separately in Section 13. "
        "Epic-type tickets are excluded from stale counts — see Section 5.5.",
        "",
        _md_table(headers, rows),
        "",
    ]

    absences = a.get("absence_data", {})
    if absences:
        total_days = 0
        pto_lines = []
        sick_lines = []
        for name in sorted(absences):
            types = absences[name]
            pto_dates = types.get("pto", [])
            sick_dates = types.get("sick", [])
            if pto_dates:
                pto_lines.append(f"{name}: {', '.join(pto_dates)} ({len(pto_dates)}d)")
                total_days += len(pto_dates)
            if sick_dates:
                sick_lines.append(f"{name}: {', '.join(sick_dates)} ({len(sick_dates)}d)")
                total_days += len(sick_dates)
        if pto_lines:
            parts.append("**PTO/OOO this cycle:** " + "; ".join(pto_lines) + ".")
        if sick_lines:
            parts.append("**Sick days this cycle:** " + "; ".join(sick_lines) + ".")
        if total_days:
            parts.append(f"Total {total_days} absence days. Reduced availability may impact throughput delta.")
        parts.append("")

    parts += ["---", ""]
    return "\n".join(parts)


def _s_activity(a: dict, config: dict, prev_activity: dict[str, int] | None = None) -> str:
    lines = [
        "## 4. Release Progress — Activity Type Split",
        "",
        "> Only tickets with Activity Type set are included. Percentages are from the categorized total only.",
        "",
    ]
    split = a.get("activity_split", {})
    targets = config.get("activity_targets", {})
    if not split:
        lines += ["No Activity Type data available.", "", "---", ""]
        return "\n".join(lines)

    total = sum(d["total"] for d in split.values())
    has_delta = bool(prev_activity)
    lines += [f"Based on {total} categorized active + recently completed tickets:", ""]

    headers = ["Activity Type", "Count", "Current %", "Target %", "Status"]
    if has_delta:
        headers.append("Delta vs previous")

    rows = []
    for name in sorted(split):
        d = split[name]
        pct = d["pct"]
        tgt = targets.get(name)
        tgt_s = f"{tgt}%" if isinstance(tgt, int | float) else "—"

        if isinstance(tgt, int | float):
            if pct >= tgt * 1.5:
                status = "Significantly over"
            elif pct >= tgt:
                status = "Over target"
            elif pct >= tgt * 0.5:
                status = "Under target"
            else:
                status = "Critically under"
        else:
            status = "—"

        row = [name, str(d["total"]), f"{pct}%", tgt_s, status]
        if has_delta:
            prev_pct = prev_activity.get(name)
            if prev_pct is not None:
                direction = "growing" if pct > prev_pct else "decreasing" if pct < prev_pct else "stable"
                row.append(f"was {prev_pct}% — {direction}")
            else:
                row.append("—")
        rows.append(row)

    lines.append(_md_table(headers, rows))
    lines.append("")

    observations = _build_activity_observations(split, targets, prev_activity)
    if observations:
        lines += ["**Observations:**"]
        for obs in observations:
            lines.append(f"- {obs}")
        lines.append("")

    lines += ["---", ""]
    return "\n".join(lines)


def _build_activity_observations(split: dict, targets: dict, prev: dict | None) -> list[str]:
    """Generate observations that add insight beyond what the table shows."""
    obs = []
    sorted_items = sorted(split.items(), key=lambda x: x[1]["pct"], reverse=True)

    if len(sorted_items) >= 2:
        top, second = sorted_items[0], sorted_items[1]
        if abs(top[1]["pct"] - second[1]["pct"]) <= 5:
            obs.append(f"{top[0]} and {second[0]} roughly balanced at {top[1]['pct']}%/{second[1]['pct']}%.")
        elif top[1]["pct"] >= 60:
            obs.append(f"{top[0]} dominates at {top[1]['pct']}% — check if other categories are under-tagged.")

    for name, d in split.items():
        tgt = targets.get(name)
        if isinstance(tgt, int | float) and d["pct"] < tgt * 0.5:
            extra = f" Only {d['total']} ticket{'s' if d['total'] != 1 else ''} categorized." if d["total"] <= 2 else ""
            obs.append(f"{name} critically below {tgt}% target.{extra}")

    if prev:
        for name, d in split.items():
            prev_pct = prev.get(name)
            if prev_pct is not None and abs(d["pct"] - prev_pct) >= 10:
                direction = "up" if d["pct"] > prev_pct else "down"
                obs.append(f"{name} shifted {direction} significantly ({prev_pct}% → {d['pct']}%).")
    return obs


def _s_risk(a: dict, prev_stale_keys: set[str] | None = None) -> str:
    lines = [
        "## 5. Risk & Attention",
        "",
        "> **Note:** Epics and Testing tickets are excluded from this section.",
        "",
    ]

    stale = a.get("stale_tickets", [])
    if stale:
        lines += ["### Stale Tickets", ""]
        rows = []
        for s in stale:
            days = s["days_in_status"]
            recurring = "Yes" if days >= 21 else "New"
            rows.append([_jl(s["key"]), s["summary"], s["assignee"], s["status"], str(days), s["level"], recurring])
        lines.append(_md_table(["Ticket", "Summary", "Assignee", "Status", "Days", "Level", "Recurring?"], rows))
    else:
        lines.append("No stale tickets this week.")
    lines.append("")

    resolved = a.get("resolved_since_last", [])
    if resolved:
        lines += ["### Resolved Since Last 360", "", "Previously stale items that were **resolved** this cycle:"]
        for r in resolved:
            lines.append(f"- {_jl(r.get('key', '?'))} — was {r.get('level', '?')} ({r.get('days_in_status', '?')}d)")
        lines.append("")
    elif prev_stale_keys:
        current_stale = {s["key"] for s in stale}
        resolved_keys = sorted(prev_stale_keys - current_stale)
        if resolved_keys:
            all_tickets = {
                t["key"]: t for t in a.get("doing_board", []) + a.get("completed", []) + a.get("backlog", [])
            }
            lines += [
                "### Resolved Since Last 360",
                "",
                f"Previously stale items no longer flagged this cycle ({len(resolved_keys)}):",
            ]
            for k in resolved_keys[:6]:
                t = all_tickets.get(k)
                if t:
                    summary = t.get("summary", "")
                    status = _get_name(t, "status")
                    lines.append(f"- {_jl(k)} — {summary} → **{status}**")
                else:
                    lines.append(f"- {_jl(k)} — resolved")
            if len(resolved_keys) > 6:
                lines.append(f"- ... and {len(resolved_keys) - 6} more")
            lines.append("")

    overloaded = a.get("overloaded_engineers", [])
    if overloaded:
        lines += ["### Overloaded Engineers", ""]
        rows = []
        for o in overloaded:
            rows.append([f"**{o['name']}**", str(o["item_count"]), ", ".join(o["signals"])])
        lines.append(_md_table(["Engineer", "Items", "Signal"], rows))
        lines.append("")

    bugs = a.get("critical_bugs", [])
    if bugs:
        lines += ["### Critical Bugs (roster)", ""]
        rows = []
        for b in bugs:
            rows.append([_jl(b["key"]), b["summary"], b["status"], b["assignee"]])
        lines.append(_md_table(["Ticket", "Summary", "Status", "Assignee"], rows))
        lines.append("")

    buried = a.get("buried_criticals", [])
    if buried:
        lines += ["### Buried Criticals", ""]
        for b in buried:
            lines.append(f"- {_jl(b['key'])} — {b['priority']} at backlog rank #{b['rank']}: {b['summary']}")
        lines.append("")

    lines += ["---", ""]
    return "\n".join(lines)


def _s_epic_progress(a: dict, prev_epics: dict[str, int] | None = None) -> str:
    lines = [
        "## 5.5. Epic Progress",
        "",
        "> Epics on the doing board. Not subject to stale detection.",
        "",
    ]
    epics = a.get("epic_progress", [])
    if not epics:
        lines += ["No Epics on the doing board.", ""]
        return "\n".join(lines)

    for ep in epics:
        key = ep.get("key", "")
        total = ep.get("total_children", 0)
        done = ep.get("done_count", 0)
        pct = round(done / total * 100) if total else 0

        completion_line = f"**Completion:** {done}/{total} done (**{pct}%**)"
        if prev_epics and key in prev_epics:
            completion_line += f" | **Delta:** was {prev_epics[key]}% → now {pct}%"

        lines += [f"### {_jl(key)} — {ep.get('summary', '')}", "", completion_line, ""]

        children = ep.get("children", [])
        if not children:
            lines.append("")
            continue

        done_children = [c for c in children if (c.get("status") or "").lower() in ("done", "closed")]
        active_children = [c for c in children if (c.get("status") or "").lower() not in ("done", "closed")]

        rows = []
        if done_children:
            rows.append(["Done", str(len(done_children)), _condense_keys([c.get("key", "") for c in done_children])])
        for c in active_children:
            assignee = _get_name(c, "assignee", "Unassigned")
            detail = f"{_jl(c.get('key', ''))} — {c.get('summary', '')} ({assignee})"
            rows.append([c.get("status", ""), "1", detail])

        if rows:
            lines.append(_md_table(["Status", "Children", "Tickets"], rows))
        if pct == 100:
            lines.append("\n> Complete. All children closed.")
        elif pct >= 80:
            remaining = [c.get("summary", "") for c in active_children]
            lines.append(f"\n> Near-complete. Remaining: {', '.join(remaining) or 'cleanup'}.")
        lines.append("")

    lines += ["---", ""]
    return "\n".join(lines)


def _condense_keys(keys: list[str]) -> str:
    if not keys:
        return ""
    if len(keys) == 1:
        return keys[0]
    prefix_match = re.match(r"^([A-Z]+-)", keys[0])
    if not prefix_match:
        return ", ".join(keys)
    prefix = prefix_match.group(1)
    parts = [keys[0]]
    for k in keys[1:]:
        parts.append(k[len(prefix) :] if k.startswith(prefix) else k)
    return ", ".join(parts)


def _s_strats(a: dict, config: dict, prev_strats: dict[str, str] | None = None) -> str:
    strats = a.get("strats", {})
    has_split = bool(config.get("strats_committed_id") or config.get("strats_planning_id"))
    signals = {s["key"]: s for s in a.get("strat_signals", [])}
    lines = ["## 6. STRAT / Feature Progress", "", "> **Release Pending STRATs are excluded.**", ""]

    def _strat_signal(key: str, status: str) -> str:
        sig = signals.get(key)
        if sig:
            return "; ".join(sig["alerts"])
        if prev_strats and key in prev_strats:
            prev_st = prev_strats[key]
            if prev_st.lower() != status.lower():
                return f"Moved to {status} (was {prev_st})"
            return "Stable"
        return "—"

    def _strat_rows(items: list, show_signals: bool = True) -> str:
        if not items:
            return "None."
        hdrs = ["Feature", "Status", "Color", "Last Comment"]
        if show_signals:
            hdrs.append("Signals")
        rows = []
        for s in items:
            k = s.get("key", "")
            summary = s.get("summary", "")
            comment = _clean_status_summary(s.get("status_summary") or "")
            row = [f"{_jl(k)} — {summary}", s.get("status", ""), s.get("color", "—"), comment]
            if show_signals:
                row.append(_strat_signal(k, s.get("status", "")))
            rows.append(row)
        return _md_table(hdrs, rows)

    if has_split:
        lines += [
            "### 5.1 Committed Work",
            "",
            "> STRATs with a fix version — actively committed.",
            "",
            _strat_rows(strats.get("committed", []), show_signals=True),
            "",
            "### 5.2 Planning Items",
            "",
            "> Informational only — no alerts.",
            "",
            _strat_rows(strats.get("planning", []), show_signals=False),
            "",
        ]
    else:
        all_s = strats.get("committed", []) + strats.get("planning", [])
        if isinstance(strats, list):
            all_s = strats
        lines += [_strat_rows(all_s), ""]

    all_strats = strats.get("committed", []) + strats.get("planning", [])
    if isinstance(strats, list):
        all_strats = strats
    key_signals = _build_strat_key_signals(all_strats, signals)
    if key_signals:
        lines += ["**Key signals:**"]
        for ks in key_signals:
            lines.append(f"- {ks}")
        lines.append("")

    lines += ["---", ""]
    return "\n".join(lines)


def _build_strat_key_signals(strats: list, signals: dict) -> list[str]:
    if not strats:
        return []
    out = []
    by_status: dict[str, list] = {}
    by_color: dict[str, int] = {}

    for s in strats:
        by_status.setdefault((s.get("status") or "").lower(), []).append(s)
        color = (s.get("color") or "").lower()
        if color:
            by_color[color] = by_color.get(color, 0) + 1

    review = by_status.get("review", []) + by_status.get("in review", [])
    if review:
        keys = [s.get("key", "") for s in review]
        out.append(f"**{len(review)} STRATs in Review** ({', '.join(keys)}) — features completing.")

    rp = by_status.get("release pending", [])
    if rp:
        out.append(f"**{len(rp)} STRATs Release Pending** — close to shipping.")

    green = by_color.get("green", 0)
    non_green = {c: n for c, n in by_color.items() if c not in ("green", "not selected", "")}
    not_selected = by_color.get("not selected", 0)

    if non_green:
        issues = ", ".join(f"{n} {c}" for c, n in non_green.items())
        out.append(f"Color issues: {issues}.")
    elif green and green + not_selected == sum(by_color.values()):
        out.append("All active STRATs remain **Green**. No regressions.")

    if not_selected:
        out.append(f"{not_selected} STRATs **Not Selected** for this release.")

    for s in strats:
        sig = signals.get(s.get("key", ""))
        if sig and any("regression" in a.lower() for a in sig.get("alerts", [])):
            out.append(f"**Regression:** {_jl(s.get('key', ''))} — color regressed.")

    return out


def _s_pr_status(a: dict) -> str:
    lines = ["## 8. PR Status", ""]

    lines += ["### 8.1 Composite PR Status by Ticket (In Review)", ""]
    review = [t for t in a.get("doing_board", []) if (t.get("status") or "").lower() in ("review", "in review")]
    if review:
        rows = []
        for t in review:
            prl = t.get("pr_links", [])
            rows.append(
                [
                    _jl(t["key"]),
                    t.get("summary", ""),
                    str(len(prl)),
                    t.get("composite_pr_status", "NO_PRS"),
                    _pr_cell(prl),
                ]
            )
        lines.append(_md_table(["Ticket", "Summary", "PRs", "Composite Status", "Details"], rows))
    else:
        lines.append("No tickets in Review.")
    lines.append("")

    lines += ["### 8.2 Per-Platform Breakdown", "", "#### GitHub PRs", ""]
    gh = a.get("github_prs", {})
    gh_rows = []
    for repo_or_cat, prs in gh.items():
        if not isinstance(prs, list):
            continue
        for pr in prs:
            if not isinstance(pr, dict) or pr.get("state", "OPEN") != "OPEN":
                continue
            url = pr.get("url", "")
            num = pr.get("number", "")
            health = pr.get("pr_health", {})
            health_str = (
                health.get("detail", health.get("category", "—")) if isinstance(health, dict) else str(health or "—")
            )
            gh_rows.append(
                [
                    f"[#{num}]({url})" if url else f"#{num}",
                    pr.get("repo", repo_or_cat),
                    pr.get("author", ""),
                    f"{pr.get('age_days', '?')}d",
                    f"{pr.get('days_since_owner_update', '?')}d",
                    health_str,
                ]
            )
    if not gh_rows:
        fallback_rows = []
        for t in a.get("doing_board", []):
            for p in t.get("pr_links", []):
                if p.get("state") == "OPEN" and p.get("platform", "github") == "github":
                    url = p.get("url", "")
                    num = url.rstrip("/").split("/")[-1] if url else "?"
                    repo = url.split("github.com/")[-1].split("/pull/")[0] if url and "github.com/" in url else "?"
                    fallback_rows.append(
                        [
                            f"[#{num}]({url})" if url else f"#{num}",
                            repo,
                            _get_name(t, "assignee", "—"),
                            f"Linked from {t['key']}",
                        ]
                    )
        if fallback_rows:
            lines.append("> PR details from ticket links (GitHub API returned no data).")
            lines.append("")
            lines.append(_md_table(["PR", "Repo", "Author", "Source"], fallback_rows))
        gh_rows = fallback_rows
    if gh_rows:
        if len(gh_rows[0]) > 4:
            lines.append(_md_table(["PR", "Repo", "Author", "Age", "Owner Update", "PR Health"], gh_rows))
    else:
        lines.append("No open GitHub PRs.")
    lines.append("")

    lines += ["#### GitLab MRs", ""]
    gl = a.get("gitlab_mrs", {})
    gl_rows = []
    for proj, mrs in gl.items():
        if not isinstance(mrs, list):
            continue
        for mr in mrs:
            if not isinstance(mr, dict):
                continue
            url = mr.get("url", "")
            num = mr.get("number", mr.get("iid", ""))
            gl_rows.append(
                [
                    f"[!{num}]({url})" if url else f"!{num}",
                    proj,
                    mr.get("author", ""),
                    mr.get("state", "open"),
                    f"{mr.get('age_days', '?')}d",
                    str(mr.get("approvals", "—")),
                    mr.get("pipeline_status", "—"),
                ]
            )
    if gl_rows:
        lines.append(_md_table(["MR", "Project", "Author", "State", "Age", "Approvals", "Pipeline"], gl_rows))
    else:
        lines.append("No open GitLab MRs.")
    lines.append("")

    lines += ["### 8.3 Alerts", ""]
    alerts = a.get("pr_alerts", [])
    if alerts:
        by_type: dict[str, list] = {}
        for al in alerts:
            by_type.setdefault(al["type"], []).append(al)
        labels = {
            "BUILD_FAILING": "Build failing",
            "PARTIALLY_MERGED": "Platform mismatch / partially merged",
            "MERGED_STILL_IN_REVIEW": "Merged but still in Review",
            "PREMATURE_TESTING": "Premature testing",
            "CHANGES_REQUESTED_NOT_ADDRESSED": "Changes requested, not addressed",
            "AWAITING_INITIAL_REVIEW": "Awaiting initial review",
        }
        for typ, items in by_type.items():
            lines.append(f"- **{labels.get(typ, typ)}:**")
            for it in items:
                lines.append(f"  - {_jl(it['key'])}: {it['detail']}")
    else:
        lines.append("No PR alerts.")
    lines.append("")
    return "\n".join(lines)


def _s_doing_board(a: dict, config: dict) -> str:
    lines = [
        "## 9. Doing Board by Person",
        "",
        "> Only roster members and unassigned tickets. Epics excluded — see Section 5.5. Third-party in Section 13.",
        "",
    ]
    pp = a.get("per_person", {})
    roster = config.get("roster", [])
    names = sorted([m["name"] for m in roster], key=lambda n: n.split()[0].lower() if n.split() else n.lower())

    for name in names:
        data = pp.get(name)
        if not data:
            continue
        lines += [f"### {name}", ""]

        if data.get("absence_data"):
            ad = data["absence_data"]
            absence_parts = []
            if ad.get("pto"):
                absence_parts.append(f"PTO/OOO: {', '.join(ad['pto'])} ({len(ad['pto'])}d)")
            if ad.get("sick"):
                absence_parts.append(f"Sick: {', '.join(ad['sick'])} ({len(ad['sick'])}d)")
            if absence_parts:
                lines += [f"**Absences:** {' | '.join(absence_parts)}", ""]

        if data.get("learning_tickets"):
            lt = data["learning_tickets"]
            keys = ", ".join(_jl(t.get("key", "?")) for t in lt)
            lines += [f"**Learning:** {keys}", ""]

        if data.get("is_qe") and data.get("testing_activity"):
            tx = data["testing_activity"]
            keys = ", ".join(_jl(t.get("key", "?")) for t in tx)
            lines += [
                f"**Testing Activity (since last 360):** "
                f"{len(tx)} ticket{'s' if len(tx) != 1 else ''} transitioned to Testing: {keys}",
                "",
            ]

        tickets = data.get("tickets", [])
        if tickets:
            rows = []
            for t in tickets:
                days = t.get("days_in_status", 0)
                level = "—"
                if not (
                    _get_name(t, "issuetype").lower() == "epic"
                    or _get_name(t, "status").lower() in ("testing", "in testing")
                    or "learning" in (t.get("summary") or "").lower()
                ):
                    level = _stale_level(days) or "OK"
                rows.append(
                    [
                        _jl(t["key"]),
                        t.get("summary", ""),
                        t.get("status", ""),
                        str(days),
                        _pr_cell(t.get("pr_links", [])),
                        level,
                    ]
                )
            lines.append(_md_table(["Ticket", "Summary", "Status", "Days Working", "PR Status", "Level"], rows))
        else:
            lines.append("No active tickets.")
        lines.append("")

        if data.get("alerts"):
            lines += [f"**Alerts:** {'; '.join(data['alerts'])}", ""]

        completions = data.get("completions", [])
        if completions:
            comp_strs = [f"{c.get('key', '')} ({c.get('summary', '')})" for c in completions[:8]]
            suffix = f" (+{len(completions) - 8} more)" if len(completions) > 8 else ""
            lines += [f"> Completed {len(completions)} tickets this cycle: {', '.join(comp_strs)}{suffix}.", ""]

    lines += ["### Unassigned", ""]
    unassigned = pp.get("Unassigned", {}).get("tickets", [])
    if unassigned:
        rows = [
            [
                _jl(t["key"]),
                t.get("summary", ""),
                t.get("status", ""),
                str(t.get("days_in_status", 0)),
                _get_name(t, "priority"),
                "Needs an owner",
            ]
            for t in unassigned
        ]
        lines.append(_md_table(["Ticket", "Summary", "Status", "Days in Status", "Priority", "Notes"], rows))
    else:
        lines.append("No unassigned tickets on the doing board.")
    lines.append("")
    return "\n".join(lines)


def _s_in_review(a: dict, config: dict) -> str:
    lines = [
        "## 10. In Review",
        "",
        "> Only roster + unassigned. Composite PR status shown.",
        "",
    ]
    roster = config.get("roster", [])
    review = [
        t
        for t in a.get("doing_board", [])
        if (t.get("status") or "").lower() in ("review", "in review") and _classify(t, roster) != "THIRD-PARTY"
    ]
    if review:
        rows = []
        for t in review:
            rows.append(
                [
                    _jl(t["key"]),
                    t.get("summary", ""),
                    _get_name(t, "assignee", "Unassigned"),
                    _pr_open_days(t.get("pr_links", [])),
                    _pr_cell(t.get("pr_links", [])),
                    t.get("composite_pr_status", "—"),
                    _pr_blocker(t),
                ]
            )
        lines.append(
            _md_table(
                ["Ticket", "Summary", "Assignee", "Days PR Open", "PR Links", "Composite Status", "Blocker"], rows
            )
        )
    else:
        lines.append("No tickets in Review.")
    lines += ["", "Legend: ✅ = merged, ⏳ = open, ❌ = closed/abandoned", ""]
    return "\n".join(lines)


def _s_backlog(a: dict) -> str:
    lines = ["## 11. Backlog Grooming", ""]
    backlog = a.get("backlog", [])
    buried_keys = {b["key"] for b in a.get("buried_criticals", [])}
    if backlog:
        rows = []
        for i, t in enumerate(backlog[:15]):
            k = t.get("key", "")
            flag = "BURIED CRITICAL" if k in buried_keys else ""
            rows.append(
                [
                    str(i + 1),
                    _jl(k),
                    t.get("summary", ""),
                    _get_name(t, "priority"),
                    _get_name(t, "assignee", "Unassigned"),
                    flag,
                ]
            )
        lines.append(_md_table(["Rank", "Ticket", "Summary", "Priority", "Assignee", "Flags"], rows))
    else:
        lines.append("Backlog is empty.")
    lines.append("")
    return "\n".join(lines)


def _s_completed(a: dict) -> str:
    lines = ["## 12. Completed Since Last 360", ""]
    completed = a.get("completed", [])
    if completed:
        rows = [
            [
                _jl(t.get("key", "")),
                t.get("summary", ""),
                _get_name(t, "assignee", "—"),
                _get_name(t, "issuetype", "—"),
            ]
            for t in completed
        ]
        lines.append(_md_table(["Ticket", "Summary", "Assignee", "Type"], rows))
        lines.append("")
        by_person: dict[str, int] = {}
        for t in completed:
            name = _get_name(t, "assignee", "—")
            by_person[name] = by_person.get(name, 0) + 1
        dist = ", ".join(f"{n} {c}" for n, c in sorted(by_person.items(), key=lambda x: -x[1]))
        lines.append(f"**Throughput:** {len(completed)} tickets completed. Distribution: {dist}.")
    else:
        lines.append("No completions since last 360.")
    lines.append("")
    return "\n".join(lines)


def _s_learning(a: dict) -> str:
    la = a.get("learning_analysis", {})
    if not la:
        return ""
    missing = la.get("missing", [])
    by_person = la.get("by_person", {})
    if not missing and not by_person:
        return ""

    lines = [
        "## 12.5. Learning Tickets",
        "",
        "> Learning tickets stay In Progress during the release cycle and are excluded from stale detection.",
        "",
    ]

    if by_person:
        rows = []
        for name in sorted(by_person):
            for t in by_person[name]:
                rows.append([name, _jl(t.get("key", "")), t.get("summary", ""), t.get("status", "")])
        lines.append(_md_table(["Assignee", "Ticket", "Summary", "Status"], rows))
        lines.append("")

    if missing:
        lines.append(f"**Missing learning ticket:** {', '.join(missing)}")
        lines.append("These roster members have no Learning ticket in the current release cycle.")
        lines.append("")

    lines += ["---", ""]
    return "\n".join(lines)


def _s_third_party(a: dict) -> str:
    lines = [
        "## 13. Third-Party Assigned Tickets",
        "",
        "> Tickets assigned to people outside the team roster.",
        "",
    ]
    tp = a.get("third_party_tickets", [])
    if tp:
        rows = [
            [
                _jl(t.get("key", "")),
                t.get("summary", ""),
                t.get("status", ""),
                _get_name(t, "assignee", "?"),
                str(t.get("days_in_status", 0)),
                "Third-party",
            ]
            for t in tp
        ]
        lines.append(_md_table(["Ticket", "Summary", "Status", "Assignee", "Days in Status", "Notes"], rows))
        lines.append("")

        def _ct(*statuses: str) -> int:
            return sum(1 for t in tp if (t.get("status") or "").lower() in statuses)

        lines += [
            "**Third-party subtotals:**",
            "",
            _md_table(
                ["Metric", "Count"],
                [
                    ["In Progress", str(_ct("in progress"))],
                    ["In Review", str(_ct("review", "in review"))],
                    ["Testing", str(_ct("testing", "in testing"))],
                    ["Total", str(len(tp))],
                ],
            ),
        ]
    else:
        lines.append("No third-party assigned tickets on the board this week.")
    lines.append("")
    return "\n".join(lines)


def _s_links(config: dict) -> str:
    lines = ["## 14. Links", "", "| Resource | Link |", "|----------|------|"]
    boards = config.get("boards", {})
    if isinstance(boards, dict):
        for name, info in boards.items():
            url = info.get("url", "") if isinstance(info, dict) else str(info)
            lines.append(f"| {name} | [{name}]({url}) |")
    for name, repo in [(r.get("stream", ""), r.get("name", "")) for r in config.get("repos", [])]:
        lines.append(f"| {name} repo | [{repo}](https://github.com/{repo}) |")
    if not boards and not config.get("repos"):
        lines.append("| No links configured | — |")
    lines.append("")
    return "\n".join(lines)


def generate_report(
    analyzed: dict,
    config: dict,
    test_mode: bool,
    prev_360_url: str | None,
    collection_time: str,
    prev_360_title: str | None = None,
    prev_report_text: str | None = None,
) -> tuple[str, str]:
    """Returns (report_markdown, emoji) so callers can use the emoji in filenames."""
    prev_snapshot = _parse_prev_snapshot(prev_report_text) if prev_report_text else None
    prev_activity = _parse_prev_activity(prev_report_text) if prev_report_text else None
    prev_epics = _parse_prev_epics(prev_report_text) if prev_report_text else None
    prev_stale = _parse_prev_stale_keys(prev_report_text) if prev_report_text else None
    prev_strats = _parse_prev_strats(prev_report_text) if prev_report_text else None

    emoji = random.choice(_TITLE_EMOJIS)
    trend = analyzed.get("trend", "")

    if analyzed.get("swimlane_filter"):
        config = {**config, "swimlane_filter": analyzed["swimlane_filter"]}

    sections = [
        _header(config, test_mode, prev_360_url, collection_time, prev_360_title, trend=trend, emoji=emoji),
        _s_wins(analyzed),
        _s_agenda(analyzed),
        _s_snapshot(analyzed, prev_snapshot=prev_snapshot),
        _s_activity(analyzed, config, prev_activity=prev_activity),
        _s_risk(analyzed, prev_stale_keys=prev_stale),
        _s_epic_progress(analyzed, prev_epics=prev_epics),
        _s_strats(analyzed, config, prev_strats=prev_strats),
        _s_pr_status(analyzed),
        _s_doing_board(analyzed, config),
        _s_in_review(analyzed, config),
        _s_backlog(analyzed),
        _s_completed(analyzed),
        _s_learning(analyzed),
        _s_third_party(analyzed),
        _s_links(config),
    ]
    md = "\n".join(s for s in sections if s)
    return _linkify_ticket_keys(md), emoji
