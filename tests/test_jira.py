from datetime import datetime, timedelta, timezone

from collectors.jira import (
    _compute_composite_pr_status,
    _days_in_status,
    _extract_urls_from_pr_field,
)

GH1 = "https://github.com/acme/repo/pull/1"
GH2 = "https://github.com/acme/repo/pull/2"
GL1 = "https://gitlab.com/acme/repo/-/merge_requests/1"


def _adf_link(url):
    """ADF paragraph whose visible text IS the url and which also carries a link mark to the same url."""
    return {
        "type": "doc",
        "content": [
            {
                "type": "paragraph",
                "content": [
                    {
                        "type": "text",
                        "text": url,
                        "marks": [{"type": "link", "attrs": {"href": url}}],
                    }
                ],
            }
        ],
    }


def test_adf_same_url_link_deduped():
    # ADF yields the url twice (text + href); result must collapse to one.
    result = _extract_urls_from_pr_field(_adf_link(GH1))
    assert result == [{"url": GH1, "platform": "github"}]


def test_two_distinct_prs():
    result = _extract_urls_from_pr_field(f"{GH1} and {GH2}")
    assert [r["url"] for r in result] == [GH1, GH2]


def test_string_classifies_platform():
    result = _extract_urls_from_pr_field(f"github {GH1} gitlab {GL1}")
    platforms = {r["url"]: r["platform"] for r in result}
    assert platforms == {GH1: "github", GL1: "gitlab"}


def test_composite_empty():
    assert _compute_composite_pr_status([]) == "NO_PRS"


def test_composite_all_merged():
    assert _compute_composite_pr_status([{"state": "MERGED"}, {"state": "MERGED"}]) == "ALL_MERGED"


def test_composite_mixed_open_and_merged():
    assert _compute_composite_pr_status([{"state": "MERGED"}, {"state": "OPEN"}]) == "PARTIALLY_MERGED"


def test_composite_all_open():
    assert _compute_composite_pr_status([{"state": "OPEN"}]) == "ALL_OPEN"


def test_jira_search_nextPageToken_pagination(monkeypatch):
    """FIX 4 regression: _jira_search must use nextPageToken, not startAt.

    Before fix: loop used startAt/isLast and silently truncated at ~100.
    After fix: loop follows nextPageToken until exhausted.
    """
    import requests

    from collectors import jira

    call_count = 0

    def mock_jira_get(session, base_url, path, params):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            # First page: 5 issues + nextPageToken
            assert "nextPageToken" not in params
            return {
                "issues": [{"key": f"T-{i}"} for i in range(1, 6)],
                "nextPageToken": "tok2",
            }
        elif call_count == 2:
            # Second page: 5 issues, no token (last page)
            assert params.get("nextPageToken") == "tok2"
            return {
                "issues": [{"key": f"T-{i}"} for i in range(6, 11)],
            }
        return {"issues": []}

    monkeypatch.setattr(jira, "_jira_get", mock_jira_get)

    session = requests.Session()
    results = jira._jira_search(session, "http://fake", "jql", max_results=100)

    # Must return all 10 issues across both pages
    assert len(results) == 10
    assert results[0]["key"] == "T-1"
    assert results[9]["key"] == "T-10"
    assert call_count == 2


def test_days_in_status_bug_case_uses_most_recent_transition():
    """Bug case: transitioned to 'In Progress' 26d ago, then 'Review' 16d ago.
    Status is 'Review' → must return 16 (NOT 26).
    """
    now = datetime.now(timezone.utc)
    in_progress_date = (now - timedelta(days=26)).replace(hour=0, minute=0, second=0, microsecond=0)
    review_date = (now - timedelta(days=16)).replace(hour=0, minute=0, second=0, microsecond=0)

    issue = {
        "fields": {
            "status": {"name": "Review"},
            "created": (now - timedelta(days=30)).replace(tzinfo=None).isoformat() + "Z",
        },
        "changelog": {
            "histories": [
                {
                    "created": in_progress_date.replace(tzinfo=None).isoformat() + "Z",
                    "items": [{"field": "status", "toString": "In Progress"}],
                },
                {
                    "created": review_date.replace(tzinfo=None).isoformat() + "Z",
                    "items": [{"field": "status", "toString": "Review"}],
                },
            ]
        },
    }

    result = _days_in_status(issue)
    assert result in (15, 16), f"Expected 16 (tolerance ±1 for date math), got {result}"


def test_days_in_status_multiple_entries_same_status_uses_latest():
    """Multiple entries in same status: entered Review 20d ago, left for In Progress 12d ago, re-entered Review 5d ago.
    Status is 'Review' → must return 5 (most recent entry).
    """
    now = datetime.now(timezone.utc)
    first_review = (now - timedelta(days=20)).replace(hour=0, minute=0, second=0, microsecond=0)
    in_progress = (now - timedelta(days=12)).replace(hour=0, minute=0, second=0, microsecond=0)
    second_review = (now - timedelta(days=5)).replace(hour=0, minute=0, second=0, microsecond=0)

    issue = {
        "fields": {
            "status": {"name": "Review"},
            "created": (now - timedelta(days=25)).replace(tzinfo=None).isoformat() + "Z",
        },
        "changelog": {
            "histories": [
                {
                    "created": first_review.replace(tzinfo=None).isoformat() + "Z",
                    "items": [{"field": "status", "toString": "Review"}],
                },
                {
                    "created": in_progress.replace(tzinfo=None).isoformat() + "Z",
                    "items": [{"field": "status", "toString": "In Progress"}],
                },
                {
                    "created": second_review.replace(tzinfo=None).isoformat() + "Z",
                    "items": [{"field": "status", "toString": "Review"}],
                },
            ]
        },
    }

    result = _days_in_status(issue)
    assert result in (4, 5), f"Expected 5 (tolerance ±1), got {result}"


def test_days_in_status_empty_changelog_uses_created():
    """Changelog empty → fallback to fields.created (8 days ago)."""
    now = datetime.now(timezone.utc)
    created_date = (now - timedelta(days=8)).replace(hour=0, minute=0, second=0, microsecond=0)

    issue = {
        "fields": {
            "status": {"name": "Backlog"},
            "created": created_date.replace(tzinfo=None).isoformat() + "Z",
        },
        "changelog": {"histories": []},
    }

    result = _days_in_status(issue)
    assert result in (7, 8), f"Expected 8 (tolerance ±1), got {result}"


def test_days_in_status_no_matching_transition_uses_created():
    """Status is 'Review' but changelog only has transitions to other statuses → fallback to created."""
    now = datetime.now(timezone.utc)
    created_date = (now - timedelta(days=10)).replace(hour=0, minute=0, second=0, microsecond=0)

    issue = {
        "fields": {
            "status": {"name": "Review"},
            "created": created_date.replace(tzinfo=None).isoformat() + "Z",
        },
        "changelog": {
            "histories": [
                {
                    "created": (now - timedelta(days=5)).replace(tzinfo=None).isoformat() + "Z",
                    "items": [{"field": "status", "toString": "In Progress"}],
                },
                {
                    "created": (now - timedelta(days=2)).replace(tzinfo=None).isoformat() + "Z",
                    "items": [{"field": "status", "toString": "Testing"}],
                },
            ]
        },
    }

    result = _days_in_status(issue)
    assert result in (9, 10), f"Expected 10 (tolerance ±1), got {result}"


def test_check_pr_status_github_open_computes_age_days(monkeypatch):
    """GitHub OPEN PR: subprocess.run returns state=OPEN and createdAt 12 days ago → age_days ~= 12."""
    import json

    from collectors import jira

    # Generate createdAt date independently (12 days ago)
    now = datetime.now(timezone.utc)
    created_date = (now - timedelta(days=12)).replace(hour=0, minute=0, second=0, microsecond=0)
    created_iso = created_date.isoformat()

    class MockCompletedProcess:
        def __init__(self):
            self.returncode = 0
            self.stdout = json.dumps(
                {
                    "state": "OPEN",
                    "mergedAt": None,
                    "url": "https://github.com/acme/repo/pull/1",
                    "createdAt": created_iso,
                }
            )

    def mock_run(*args, **kwargs):
        return MockCompletedProcess()

    monkeypatch.setattr("subprocess.run", mock_run)

    result = jira._check_pr_status("https://github.com/acme/repo/pull/1", "github")

    assert result["state"] == "OPEN", f"Expected state OPEN, got {result['state']}"
    assert result["checked"] is True, "Expected checked to be True"
    assert "age_days" in result, "age_days should be present"
    assert abs(result["age_days"] - 12) <= 1, f"Expected age_days ~= 12 (±1), got {result['age_days']}"


def test_check_pr_status_github_merged(monkeypatch):
    """GitHub MERGED PR: subprocess.run returns mergedAt set → state == MERGED."""
    import json

    from collectors import jira

    class MockCompletedProcess:
        def __init__(self):
            self.returncode = 0
            self.stdout = json.dumps(
                {
                    "state": "MERGED",
                    "mergedAt": "2026-08-15T10:00:00Z",
                    "url": "https://github.com/acme/repo/pull/2",
                    "createdAt": "2026-08-01T10:00:00Z",
                }
            )

    def mock_run(*args, **kwargs):
        return MockCompletedProcess()

    monkeypatch.setattr("subprocess.run", mock_run)

    result = jira._check_pr_status("https://github.com/acme/repo/pull/2", "github")

    assert result["state"] == "MERGED", f"Expected state MERGED, got {result['state']}"
    assert result["checked"] is True, "Expected checked to be True"


def test_check_pr_status_github_error_returncode(monkeypatch):
    """GitHub PR check fails (returncode != 0) → state=UNKNOWN, checked=False, no age_days."""
    from collectors import jira

    class MockCompletedProcess:
        def __init__(self):
            self.returncode = 1
            self.stdout = ""

    def mock_run(*args, **kwargs):
        return MockCompletedProcess()

    monkeypatch.setattr("subprocess.run", mock_run)

    result = jira._check_pr_status("https://github.com/acme/repo/pull/999", "github")

    assert result["state"] == "UNKNOWN", f"Expected state UNKNOWN, got {result['state']}"
    assert result["checked"] is False, "Expected checked to be False"
    assert "age_days" not in result, "age_days should not be present on error"


def test_check_pr_status_gitlab_open_computes_age_days(monkeypatch):
    """GitLab OPEN MR: subprocess.run returns state=opened and created_at 7 days ago → age_days ~= 7."""
    import json

    from collectors import jira

    # Generate created_at date independently (7 days ago)
    now = datetime.now(timezone.utc)
    created_date = (now - timedelta(days=7)).replace(hour=0, minute=0, second=0, microsecond=0)
    created_iso = created_date.isoformat()

    class MockCompletedProcess:
        def __init__(self):
            self.returncode = 0
            self.stdout = json.dumps(
                {
                    "state": "opened",
                    "created_at": created_iso,
                }
            )

    def mock_run(*args, **kwargs):
        return MockCompletedProcess()

    monkeypatch.setattr("subprocess.run", mock_run)
    monkeypatch.setenv("GITLAB_HOST", "gitlab.example.com")

    result = jira._check_pr_status("https://gitlab.example.com/group/proj/-/merge_requests/5", "gitlab")

    assert result["state"] == "OPEN", f"Expected state OPEN, got {result['state']}"
    assert result["checked"] is True, "Expected checked to be True"
    assert "age_days" in result, "age_days should be present"
    assert abs(result["age_days"] - 7) <= 1, f"Expected age_days ~= 7 (±1), got {result['age_days']}"
