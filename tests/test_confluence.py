from publishers.confluence import _v2_base


def test_v2_base_with_wiki_not_duplicated():
    assert _v2_base("https://example.atlassian.net/wiki") == "https://example.atlassian.net/wiki/api/v2"


def test_v2_base_with_wiki_trailing_slash():
    assert _v2_base("https://example.atlassian.net/wiki/") == "https://example.atlassian.net/wiki/api/v2"


def test_v2_base_without_wiki_adds_it():
    assert _v2_base("https://example.atlassian.net") == "https://example.atlassian.net/wiki/api/v2"
