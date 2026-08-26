"""Regression tests for heuristics.py bug fixes."""

from heuristics import _analyze_prs, apply_heuristics


def test_analyze_prs_dict_health_regression():
    """FIX 1 regression: pr_health is a dict with category+detail, not a string.

    Before fix: health = pr.get("pr_health", "") compared dict to string → always False.
    After fix: health = (pr.get("pr_health") or {}).get("category") → alerts fire correctly.
    """
    board = []
    gh_prs = {
        "upstream": [
            {
                "number": 7,
                "url": "http://example.com/pull/7",
                "pr_health": {"category": "BUILD_FAILING", "detail": "Build failing: ci"},
                "days_since_owner_update": 2,
            }
        ]
    }
    roster = []

    alerts = _analyze_prs(board, gh_prs, roster)

    # Must produce exactly one alert
    assert len(alerts) == 1
    alert = alerts[0]
    assert alert["type"] == "BUILD_FAILING"
    assert alert["detail"] == "Build failing: ci"
    assert alert["url"] == "http://example.com/pull/7"


def test_wire_pr_health_into_ticket_pr_links():
    """FIX 3 regression: ticket pr_links must get real pr_health from github_prs.

    Before fix: ticket pr_links had no pr_health (only url/state/platform).
    After fix: apply_heuristics wires pr_health from github_prs into ticket pr_links.
    """
    board = [
        {
            "key": "T-1",
            "pr_links": [{"url": "http://example.com/pull/7", "state": "OPEN", "platform": "github"}],
        }
    ]
    github_prs = {
        "roster_prs": [
            {
                "url": "http://example.com/pull/7",
                "pr_health": {"category": "BUILD_FAILING", "detail": "Build failing: ci"},
            }
        ]
    }
    config = {"roster": []}

    result = apply_heuristics(
        doing_board=board,
        backlog=[],
        strats={"committed": [], "planning": []},
        completed=[],
        github_prs=github_prs,
        gitlab_mrs={},
        testing_transitions=[],
        epic_progress=[],
        config=config,
        previous_360=None,
    )

    # After wiring, ticket pr_link should have pr_health and pr_health_detail
    ticket = result["doing_board"][0]
    pr_link = ticket["pr_links"][0]
    assert pr_link["pr_health"] == "BUILD_FAILING"
    assert pr_link["pr_health_detail"] == "Build failing: ci"
