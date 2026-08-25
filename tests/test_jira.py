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
