"""Non-tautological tests for report.py linkify and summary truncation fixes."""

from unittest.mock import patch

import report


def test_linkify_wraps_raw_key():
    """Raw ticket key in text becomes a proper markdown link."""
    with patch("report._jira_base", return_value="https://jira.example.com"):
        result = report._linkify_ticket_keys("Working on RHOAIENG-123 today")
        expected = "Working on [RHOAIENG-123](https://jira.example.com/browse/RHOAIENG-123) today"
        assert result == expected, f"Expected exact link, got: {result}"


def test_linkify_leaves_existing_link_intact():
    """Already-linked ticket is not double-wrapped."""
    input_text = "See [RHOAIENG-123](https://jira.example.com/browse/RHOAIENG-123) for details"
    with patch("report._jira_base", return_value="https://jira.example.com"):
        result = report._linkify_ticket_keys(input_text)
        assert result == input_text, "Existing link should remain unchanged"
        # Verify no double-wrapping occurred
        assert result.count("browse/RHOAIENG-123") == 1, "Key should appear in URL exactly once"
        assert "browse/RHOAIENG-123](" not in result, "No double-wrap pattern should exist"


def test_linkify_no_base_returns_unchanged():
    """When no Jira base URL is configured, text is returned unchanged."""
    input_text = "RHOAIENG-123 is a ticket"
    with patch("report._jira_base", return_value=""):
        result = report._linkify_ticket_keys(input_text)
        assert result == input_text, "Text should be unchanged when base URL is empty"


def test_clean_status_summary_not_truncated():
    """Long clean comment text is NOT truncated at 60 chars."""
    # Build a clean comment > 60 chars with no prefixes to strip
    long_comment = "This is a detailed status update that explains the current progress and next steps for the team to review carefully"
    assert len(long_comment) > 60, "Test input must be > 60 chars"

    result = report._clean_status_summary(long_comment)

    # Assert full text is preserved
    assert "..." not in result, "Result should not contain truncation ellipsis"
    assert result == long_comment, "Full comment should be preserved without truncation"
    assert len(result) == len(long_comment), "Length should match original"


def test_enrich_agenda_summary_not_truncated():
    """Long summary in ticket dict is NOT truncated when enriching agenda text."""
    # Build a summary > 70 chars
    long_summary = (
        "This is a very long ticket summary that describes the feature in detail and should not be truncated at all"
    )
    assert len(long_summary) > 70, "Test summary must be > 70 chars"

    ticket = {
        "summary": long_summary,
        "status": "In Progress",
        "days_in_status": 5,
        "assignee": "Engineer Name",
        "pr_links": [],
    }

    result = report._enrich_agenda_text("RHOAIENG-999", ticket)

    # Assert the full summary appears in the enriched text (no truncation)
    assert long_summary in result, "Full summary should appear in enriched text"
    # Verify no "..." truncation indicator from summary truncation
    # (Note: result may contain "..." from other parts, but not as summary[:67] + "...")
    # Check that the last words of the summary are present
    last_words = long_summary.split()[-3:]  # Last 3 words
    for word in last_words:
        assert word in result, f"Last word '{word}' of summary should be present, indicating no truncation"


def test_pr_open_days_single_open():
    """Single OPEN PR with age_days returns the age as string."""
    pr_links = [{"url": "https://github.com/acme/repo/pull/1", "state": "OPEN", "age_days": 12}]
    result = report._pr_open_days(pr_links)
    assert result == "12", f"Expected '12', got {result}"


def test_pr_open_days_multiple_open_returns_max():
    """Multiple OPEN PRs return the oldest (max age_days)."""
    pr_links = [
        {"url": "https://github.com/acme/repo/pull/1", "state": "OPEN", "age_days": 5},
        {"url": "https://github.com/acme/repo/pull/2", "state": "OPEN", "age_days": 20},
    ]
    result = report._pr_open_days(pr_links)
    assert result == "20", f"Expected '20' (max age), got {result}"


def test_pr_open_days_excludes_non_open_states():
    """PRs with state != OPEN (MERGED/CLOSED) are excluded, even if they have age_days."""
    pr_links = [
        {"url": "https://github.com/acme/repo/pull/1", "state": "MERGED", "age_days": 30},
        {"url": "https://github.com/acme/repo/pull/2", "state": "CLOSED", "age_days": 25},
        {"url": "https://github.com/acme/repo/pull/3", "state": "OPEN"},
    ]
    result = report._pr_open_days(pr_links)
    assert result == "—", f"Expected '—' (no OPEN with age_days), got {result}"


def test_pr_open_days_missing_age_days():
    """OPEN PR without age_days field returns em-dash."""
    pr_links = [{"url": "https://github.com/acme/repo/pull/1", "state": "OPEN"}]
    result = report._pr_open_days(pr_links)
    assert result == "—", f"Expected '—' (age_days missing), got {result}"


def test_pr_open_days_empty_list():
    """Empty pr_links list returns em-dash."""
    result = report._pr_open_days([])
    assert result == "—", f"Expected '—' (empty list), got {result}"
