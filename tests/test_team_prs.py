from team_prs import _dedupe_prs, _match_to_roster, _review_status

ROSTER = [{"name": "Alice Smith"}, {"name": "Bob Jones"}]


def test_review_status_approved():
    assert _review_status("APPROVED") == "Approved"


def test_review_status_changes_requested():
    assert _review_status("CHANGES_REQUESTED") == "Changes Requested"


def test_review_status_waiting_variants():
    assert _review_status("REVIEW_REQUIRED") == "Waiting for Review"
    assert _review_status("") == "Waiting for Review"
    assert _review_status(None) == "Waiting for Review"


def test_match_substring():
    assert _match_to_roster("alice", ROSTER) == "Alice Smith"


def test_match_first_name_branch():
    # Full name is not a substring either way; only the first-name prefix rule can match.
    assert _match_to_roster("bob in accounting", ROSTER) == "Bob Jones"


def test_match_none():
    assert _match_to_roster("charlie", ROSTER) is None
    assert _match_to_roster("", ROSTER) is None


def test_dedupe_keeps_older_and_propagates_jira_key():
    url = "https://github.com/acme/repo/pull/1"
    prs = [
        {"url": url, "age_days": 2, "jira_key": "PROJ-1"},
        {"url": url, "age_days": 5, "jira_key": None},
    ]
    result = _dedupe_prs(prs)
    assert len(result) == 1
    # Older PR (larger age_days) is kept, jira_key propagated from the earlier entry.
    assert result[0]["age_days"] == 5
    assert result[0]["jira_key"] == "PROJ-1"


def test_dedupe_distinct_urls_kept():
    prs = [
        {"url": "https://github.com/acme/repo/pull/1", "age_days": 1},
        {"url": "https://github.com/acme/repo/pull/2", "age_days": 1},
    ]
    assert len(_dedupe_prs(prs)) == 2
