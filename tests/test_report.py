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
        "days_worked": 5,
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
