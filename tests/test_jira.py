from collectors.jira import (
    _compute_composite_pr_status,
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
