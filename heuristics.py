"""Heuristic analysis engine for 360 reports."""

from __future__ import annotations

import logging
import os

log = logging.getLogger(__name__)

STALE_THRESHOLDS = [(14, "DANGER"), (7, "WARNING"), (3, "ATTENTION")]
COLOR_RANK = {"green": 0, "yellow": 1, "orange": 2, "red": 3}


def _jira_base() -> str:
    return (os.environ.get("JIRA_BASE_URL") or os.environ.get("JIRA_URL") or "").rstrip("/")


def _get_assignee_name(ticket: dict) -> str | None:
    raw = ticket.get("assignee")
    if not raw:
        return None
    if isinstance(raw, dict):
        return raw.get("displayName")
    return str(raw) if raw != "UNASSIGNED" else None


def _get_field_str(ticket: dict, field: str, sub: str = "name") -> str:
    raw = ticket.get(field, "")
    if isinstance(raw, dict):
        return raw.get(sub, "")
    return str(raw) if raw else ""


def _stale_level(days: int) -> str | None:
    for threshold, label in STALE_THRESHOLDS:
        if days >= threshold:
            return label
    return None


def _roster_match(name: str | None, roster: list[dict]) -> str | None:
    if not name:
        return None
    low = name.lower()
    for m in roster:
        mlow = m["name"].lower()
        if low in mlow or mlow in low:
            return m["name"]
    return None


def _classify(ticket: dict, roster: list[dict]) -> str:
    raw = ticket.get("assignee")
    if isinstance(raw, dict):
        assignee = raw.get("displayName")
    else:
        assignee = raw
    if not assignee:
        return "UNASSIGNED"
    if ticket.get("classification"):
        return ticket["classification"]
    return "ROSTER" if _roster_match(assignee, roster) else "THIRD-PARTY"


def _is_epic(t: dict) -> bool:
    it = t.get("issuetype", "")
    if isinstance(it, dict):
        return (it.get("name") or "").lower() == "epic"
    return str(it).lower() == "epic"


def _is_testing(t: dict) -> bool:
    s = t.get("status", "")
    if isinstance(s, dict):
        s = s.get("name", "")
    return str(s).lower() in ("testing", "in testing")


def _is_strat(t: dict) -> bool:
    return t.get("key", "").startswith("RHAISTRAT")


def _is_learning(t: dict) -> bool:
    return "learning" in (t.get("summary") or "").lower()


def _is_qe(m: dict) -> bool:
    return (m.get("role") or "").strip().lower().endswith("qe")


def _epic_key(t: dict) -> str:
    return t.get("parent_key") or t.get("parent_epic") or t.get("epic_key") or ""


def _ticket_link(key: str) -> str:
    base = _jira_base()
    return f"[{key}]({base}/browse/{key})" if base else key


def _find_stale(board: list, roster: list[dict]) -> list[dict]:
    out = []
    for t in board:
        if _is_epic(t) or _is_testing(t) or _is_strat(t) or _is_learning(t):
            continue
        if _classify(t, roster) == "THIRD-PARTY":
            continue
        days = t.get("days_worked") or 0
        level = _stale_level(days)
        if level:
            out.append(
                {
                    "key": t["key"],
                    "summary": t.get("summary", ""),
                    "status": t.get("status", ""),
                    "assignee": _get_assignee_name(t) or "Unassigned",
                    "days_worked": days,
                    "level": level,
                    "priority": _get_field_str(t, "priority"),
                }
            )
    out.sort(key=lambda x: x["days_worked"], reverse=True)
    return out


def _find_overloaded(board: list, roster: list[dict]) -> list[dict]:
    by_person: dict[str, list[dict]] = {}
    for t in board:
        if _is_epic(t) or _is_learning(t):
            continue
        name = _roster_match(_get_assignee_name(t), roster)
        if name:
            by_person.setdefault(name, []).append(t)

    out = []
    for person, tickets in by_person.items():
        signals: list[str] = []
        epics: dict[str, list] = {}
        for t in tickets:
            ek = _epic_key(t)
            if ek:
                epics.setdefault(ek, []).append(t)

        orphans = sum(1 for t in tickets if not _epic_key(t))
        non_sibling = len(epics) + orphans

        if non_sibling >= 4:
            signals.append(f"Hard WIP ({non_sibling} non-sibling items)")
        if len(tickets) >= 3 and len(epics) >= 2:
            signals.append(f"Context-switching ({len(tickets)} items across {len(epics)} epics)")

        stale_epic_set = {_epic_key(t) for t in tickets if (t.get("days_worked") or 0) >= 7 and _epic_key(t)}
        if len(stale_epic_set) >= 2:
            signals.append(f"Spread thin + stale across {len(stale_epic_set)} epics")

        blocked = [
            t
            for t in tickets
            if (t.get("status") or "").lower() == "blocked"
            or "blocked" in [(la or "").lower() for la in (t.get("labels") or [])]
            or t.get("is_blocked")
        ]
        if blocked:
            signals.append(f"Blocked ({len(blocked)})")

        if signals:
            out.append({"name": person, "signals": signals, "item_count": len(tickets)})
    return out


def _find_critical_bugs(board: list, roster: list[dict]) -> list[dict]:
    return [
        {
            "key": t["key"],
            "summary": t.get("summary", ""),
            "priority": _get_field_str(t, "priority"),
            "assignee": _get_assignee_name(t) or "Unassigned",
            "status": t.get("status", ""),
            "classification": _classify(t, roster),
        }
        for t in board
        if _get_field_str(t, "issuetype").lower() == "bug"
        and _get_field_str(t, "priority").lower() in ("critical", "blocker")
        and _classify(t, roster) != "THIRD-PARTY"
    ]


def _find_buried_criticals(backlog: list) -> list[dict]:
    return [
        {"key": t["key"], "summary": t.get("summary", ""), "priority": _get_field_str(t, "priority"), "rank": i + 1}
        for i, t in enumerate(backlog)
        if i >= 5 and _get_field_str(t, "priority").lower() in ("critical", "blocker")
    ]


def _analyze_strats(strats: dict, prev: dict | None) -> list[dict]:
    committed = strats.get("committed", [])
    prev_colors = (prev or {}).get("strat_colors", {})
    signals = []
    for s in committed:
        key = s.get("key", "")
        color = (s.get("color") or "").lower()
        alerts: list[str] = []
        if not color:
            alerts.append("Missing color — no status update")
        if key in prev_colors:
            pc = prev_colors[key].lower()
            if color and COLOR_RANK.get(color, -1) > COLOR_RANK.get(pc, -1):
                alerts.append(f"Regression: {pc} -> {color}")
            elif color and color == pc and pc != "green":
                alerts.append(f"Stall: {color} for 2+ reports")
        if alerts:
            signals.append(
                {
                    "key": key,
                    "summary": s.get("summary", ""),
                    "color": s.get("color", ""),
                    "status": s.get("status", ""),
                    "alerts": alerts,
                }
            )
    return signals


def _analyze_prs(board: list, gh_prs: dict, roster: list[dict]) -> list[dict]:
    alerts = []
    for t in board:
        if _classify(t, roster) == "THIRD-PARTY":
            continue
        composite = t.get("composite_pr_status", "")
        pr_links = t.get("pr_links", [])
        status = (t.get("status") or "").lower()

        if composite == "PARTIALLY_MERGED":
            merged = sum(1 for p in pr_links if p.get("state") == "MERGED")
            alerts.append(
                {
                    "key": t["key"],
                    "type": "PARTIALLY_MERGED",
                    "detail": f"{merged}/{len(pr_links)} merged — others still open",
                }
            )
        if status in ("review", "in review") and composite == "ALL_MERGED":
            alerts.append(
                {
                    "key": t["key"],
                    "type": "MERGED_STILL_IN_REVIEW",
                    "detail": "All PRs merged but ticket still in Review",
                }
            )
        if status in ("testing", "in testing") and composite in ("ALL_OPEN", "PARTIALLY_MERGED"):
            alerts.append(
                {"key": t["key"], "type": "PREMATURE_TESTING", "detail": "Moved to Testing before all PRs merged"}
            )

    for repo, prs in gh_prs.items():
        for pr in prs:
            health = (pr.get("pr_health") or {}).get("category")
            if health in ("BUILD_FAILING", "CHANGES_REQUESTED_NOT_ADDRESSED", "AWAITING_INITIAL_REVIEW"):
                alerts.append(
                    {
                        "key": f"#{pr.get('number', '')}",
                        "type": health,
                        "detail": (pr.get("pr_health") or {}).get("detail", ""),
                        "repo": repo,
                        "url": pr.get("url", ""),
                        "days_since_owner_update": pr.get("days_since_owner_update", 0),
                    }
                )
    return alerts


def _activity_split(board: list, completed: list) -> dict:
    completed_keys = {t.get("key") for t in completed}
    counts: dict[str, dict[str, int]] = {}
    for t in board + completed:
        at = t.get("activity_type")
        if isinstance(at, dict):
            at = at.get("value")
        if not at:
            continue
        bucket = counts.setdefault(at, {"active": 0, "completed": 0})
        if t.get("key") in completed_keys:
            bucket["completed"] += 1
        else:
            bucket["active"] += 1

    total = sum(c["active"] + c["completed"] for c in counts.values())
    return {
        name: {
            **c,
            "total": c["active"] + c["completed"],
            "pct": round((c["active"] + c["completed"]) / total * 100) if total else 0,
        }
        for name, c in counts.items()
    }


def _build_per_person(board: list, roster: list[dict], stale: list[dict], testing_tx: list, completed: list) -> dict:
    stale_keys = {s["key"] for s in stale}
    pp: dict[str, dict] = {}
    for m in roster:
        pp[m["name"]] = {"tickets": [], "alerts": [], "testing_activity": [], "completions": [], "is_qe": _is_qe(m)}
    pp["Unassigned"] = {"tickets": [], "alerts": [], "testing_activity": [], "completions": [], "is_qe": False}

    for t in board:
        cls = _classify(t, roster)
        if cls == "THIRD-PARTY":
            continue
        if cls == "UNASSIGNED":
            pp["Unassigned"]["tickets"].append(t)
            continue
        name = _roster_match(_get_assignee_name(t), roster)
        if name and name in pp:
            pp[name]["tickets"].append(t)
            if t["key"] in stale_keys:
                pp[name]["alerts"].append(f"{t['key']} stale ({t.get('days_worked', 0)}d)")

    qe_members = [m for m in roster if _is_qe(m)]
    for tt in testing_tx:
        transitioned_by = tt.get("transitioned_by", "")
        assigned = False
        if transitioned_by:
            matched = _roster_match(transitioned_by, qe_members)
            if matched and matched in pp:
                pp[matched]["testing_activity"].append(tt)
                assigned = True
        if not assigned:
            for m in qe_members:
                if m["name"] in pp:
                    pp[m["name"]]["testing_activity"].append(tt)

    for c in completed:
        name = _roster_match(_get_assignee_name(c), roster)
        if name and name in pp:
            pp[name]["completions"].append(c)

    return pp


def _build_agenda(
    stale: list,
    overloaded: list,
    bugs: list,
    pr_alerts: list,
    strat_sig: list,
    board: list,
    roster: list[dict],
    prev: dict | None,
) -> list[dict]:
    agenda: list[dict] = []
    prev_stale_keys = {s.get("key") for s in (prev or {}).get("stale_items", [])}
    prev_date = (prev or {}).get("date", "previous 360")

    for s in stale:
        if s["key"] in prev_stale_keys:
            agenda.append(
                {
                    "priority": 1,
                    "text": f"{_ticket_link(s['key'])} — {s['summary']} — "
                    f"{s['level']} ({s['days_worked']}d, carried over from {prev_date})",
                }
            )

    for b in bugs:
        if b["classification"] == "UNASSIGNED":
            agenda.append(
                {"priority": 2, "text": f"{_ticket_link(b['key'])} — {b['priority']} bug unassigned: {b['summary']}"}
            )

    for o in overloaded:
        agenda.append({"priority": 3, "text": f"{o['name']} — {', '.join(o['signals'])}"})

    for s in stale:
        if s["key"] not in prev_stale_keys and s["level"] in ("WARNING", "DANGER"):
            agenda.append(
                {
                    "priority": 4,
                    "text": f"{_ticket_link(s['key'])} — {s['summary']} — {s['level']} ({s['days_worked']}d)",
                }
            )

    for a in pr_alerts:
        if a["type"] == "PARTIALLY_MERGED":
            agenda.append({"priority": 5, "text": f"{_ticket_link(a['key'])} — {a['detail']}"})

    for a in pr_alerts:
        if a["type"] == "AWAITING_INITIAL_REVIEW" and a.get("days_since_owner_update", 0) >= 14:
            url_or_key = a.get("url") or a["key"]
            agenda.append({"priority": 6, "text": f"{url_or_key} — {a['detail']}"})

    for s in strat_sig:
        for alert in s["alerts"]:
            agenda.append({"priority": 7, "text": f"{_ticket_link(s['key'])} — {s['summary']} — {alert}"})

    seen_keys = set()
    for a in agenda:
        for part in a["text"].split("]"):
            if "[" in part:
                k = part.split("[")[-1]
                seen_keys.add(k)
    for t in board:
        if _classify(t, roster) == "UNASSIGNED" and not _is_epic(t) and not _is_strat(t) and t["key"] not in seen_keys:
            agenda.append({"priority": 8, "text": f"{_ticket_link(t['key'])} — {t.get('summary', '')} — needs owner"})

    agenda.sort(key=lambda x: x["priority"])
    return agenda[:8]


def _build_snapshot(board: list, stale: list, bugs: list, completed: list, roster: list[dict]) -> dict:
    own = [t for t in board if _classify(t, roster) != "THIRD-PARTY"]
    tp = [t for t in board if _classify(t, roster) == "THIRD-PARTY"]

    def _count(*statuses: str) -> int:
        return sum(1 for t in own if (t.get("status") or "").lower() in statuses)

    engineers = {
        _roster_match(_get_assignee_name(t), roster) for t in own if _roster_match(_get_assignee_name(t), roster)
    }

    return {
        "in_progress": _count("in progress"),
        "in_review": _count("review", "in review"),
        "testing": _count("testing", "in testing"),
        "total_doing": len(own),
        "unassigned": sum(1 for t in own if _classify(t, roster) == "UNASSIGNED"),
        "stale": sum(1 for s in stale if s["level"] in ("WARNING", "DANGER")),
        "critical_bugs": len(bugs),
        "completed": len(completed),
        "active_engineers": len(engineers),
        "third_party": len(tp),
    }


def _build_trend(snapshot: dict, completed: list, strats: dict, stale: list, prev: dict | None) -> str:
    if not prev:
        return "First 360 for this team. No comparison baseline."

    parts = []
    prev_snap = prev.get("snapshot_stats", {})

    prev_total = prev_snap.get("total_doing", 0)
    cur_total = snapshot.get("total_doing", 0)
    if prev_total and cur_total != prev_total:
        delta = cur_total - prev_total
        pct = round(abs(delta) / prev_total * 100) if prev_total else 0
        direction = "up" if delta > 0 else "down"
        parts.append(f"Board went from {prev_total} to {cur_total} ({direction} {pct}%).")

    epics_closed = sum(1 for c in completed if _is_epic(c))
    if epics_closed:
        parts.append(f"{epics_closed} Epic{'s' if epics_closed != 1 else ''} closed.")

    rp = [
        s
        for s in strats.get("committed", [])
        if (s.get("status") or "").lower() in ("release pending", "done", "closed")
    ]
    if rp:
        parts.append(f"{len(rp)} STRAT{'s' if len(rp) != 1 else ''} to Release Pending.")

    prev_stale_ct = prev_snap.get("stale", 0)
    cur_stale_ct = snapshot.get("stale", 0)
    if cur_stale_ct != prev_stale_ct:
        parts.append(
            f"Stale count {'up' if cur_stale_ct > prev_stale_ct else 'down'} from {prev_stale_ct} to {cur_stale_ct}."
        )

    return " ".join(parts) if parts else "Board stable. No significant movement this cycle."


def _build_wins(completed: list, strats: dict, resolved: list, bugs_prev: list, bugs_cur: list) -> list[dict]:
    wins: list[dict] = []

    for c in completed:
        if _is_epic(c):
            wins.append({"win": f"{c['key']} ({c.get('summary', '')}) closed", "who": _get_assignee_name(c) or "Team"})

    for s in strats.get("committed", []):
        if (s.get("status") or "").lower() in ("release pending", "done", "closed"):
            wins.append(
                {
                    "win": f"{s['key']} ({s.get('summary', '')}) moved to Release Pending",
                    "who": s.get("assignee") or s.get("owner") or "Team",
                }
            )

    for r in resolved:
        wins.append(
            {
                "win": f"{r['key']} ({r.get('summary', '')}) resolved after being stale",
                "who": r.get("assignee") or "Team",
            }
        )

    prev_bug_keys = {b.get("key") for b in bugs_prev}
    for bk in prev_bug_keys:
        if bk and bk not in {b.get("key") for b in bugs_cur}:
            wins.append({"win": f"{bk} critical bug fixed", "who": "Team"})

    return wins[:5]


def _analyze_learning(learning_tickets: list[dict], roster: list[dict]) -> dict:
    """Check which roster members have a learning ticket for this cycle."""
    by_person: dict[str, list[dict]] = {}
    for t in learning_tickets:
        name = _roster_match(_get_assignee_name(t), roster)
        if name:
            by_person.setdefault(name, []).append(t)

    missing = [m["name"] for m in roster if m["name"] not in by_person]
    return {"by_person": by_person, "missing": missing}


def apply_heuristics(
    doing_board: list,
    backlog: list,
    strats: dict,
    completed: list,
    github_prs: dict,
    gitlab_mrs: dict,
    testing_transitions: list,
    epic_progress: list,
    config: dict,
    previous_360: dict | None,
    absence_data: dict[str, dict[str, list[str]]] | None = None,
    learning_tickets: list[dict] | None = None,
) -> dict:
    """Apply all heuristic rules and return analyzed data."""
    roster = config.get("roster", [])

    # Wire real PR health into ticket pr_links (FIX 3)
    pr_health_map: dict[str, dict] = {}
    for prs in github_prs.values():
        for pr in prs:
            url = pr.get("url")
            if url and pr.get("pr_health"):
                pr_health_map[url] = pr["pr_health"]

    for ticket in doing_board:
        for pr_link in ticket.get("pr_links", []):
            url = pr_link.get("url")
            if url in pr_health_map:
                health_dict = pr_health_map[url]
                pr_link["pr_health"] = health_dict.get("category", "")
                pr_link["pr_health_detail"] = health_dict.get("detail", "")

    stale = _find_stale(doing_board, roster)
    overloaded = _find_overloaded(doing_board, roster)
    bugs = _find_critical_bugs(doing_board, roster)
    buried = _find_buried_criticals(backlog)
    strat_signals = _analyze_strats(strats, previous_360)
    pr_alerts = _analyze_prs(doing_board, github_prs, roster)
    prev_stale = (previous_360 or {}).get("stale_items", [])
    resolved = [s for s in prev_stale if s.get("key") not in {t["key"] for t in doing_board}]
    per_person = _build_per_person(doing_board, roster, stale, testing_transitions, completed)

    # Enrich per-person with absence data (PTO/OOO/Sick)
    if absence_data:
        for name, types in absence_data.items():
            matched = _roster_match(name, roster)
            if matched and matched in per_person:
                per_person[matched]["absence_data"] = types

    # Learning ticket analysis
    learning_analysis = _analyze_learning(learning_tickets or [], roster)
    for name, tickets in learning_analysis["by_person"].items():
        if name in per_person:
            per_person[name]["learning_tickets"] = tickets

    agenda = _build_agenda(stale, overloaded, bugs, pr_alerts, strat_signals, doing_board, roster, previous_360)
    snapshot = _build_snapshot(doing_board, stale, bugs, completed, roster)
    activity = _activity_split(doing_board, completed)
    third_party = [t for t in doing_board if _classify(t, roster) == "THIRD-PARTY"]
    trend = _build_trend(snapshot, completed, strats, stale, previous_360)
    prev_bugs = (previous_360 or {}).get("critical_bugs", [])
    wins = _build_wins(completed, strats, resolved, prev_bugs, bugs)

    return {
        "stale_tickets": stale,
        "overloaded_engineers": overloaded,
        "critical_bugs": bugs,
        "buried_criticals": buried,
        "strat_signals": strat_signals,
        "pr_alerts": pr_alerts,
        "resolved_since_last": resolved,
        "per_person": per_person,
        "agenda": agenda,
        "snapshot_stats": snapshot,
        "epic_progress": epic_progress,
        "strats": strats,
        "doing_board": doing_board,
        "backlog": backlog,
        "completed": completed,
        "testing_transitions": testing_transitions,
        "github_prs": github_prs,
        "gitlab_mrs": gitlab_mrs,
        "third_party_tickets": third_party,
        "activity_split": activity,
        "trend": trend,
        "wins": wins,
        "absence_data": absence_data or {},
        "learning_analysis": learning_analysis,
    }
