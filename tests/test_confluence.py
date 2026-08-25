import publishers.confluence as conf
from publishers.confluence import _v2_base, fetch_previous_360


def test_v2_base_with_wiki_not_duplicated():
    assert _v2_base("https://example.atlassian.net/wiki") == "https://example.atlassian.net/wiki/api/v2"


def test_v2_base_with_wiki_trailing_slash():
    assert _v2_base("https://example.atlassian.net/wiki/") == "https://example.atlassian.net/wiki/api/v2"


def test_v2_base_without_wiki_adds_it():
    assert _v2_base("https://example.atlassian.net") == "https://example.atlassian.net/wiki/api/v2"


class _Resp:
    def __init__(self, data):
        self._data = data

    def json(self):
        return self._data

    def raise_for_status(self):
        pass


def test_fetch_previous_360_skips_test_page(monkeypatch):
    captured = {}

    def fake_get(url, **kwargs):
        captured["url"] = url
        captured["params"] = kwargs.get("params", {})
        # Newest first: the test page must be skipped in favour of the real 19/08 page.
        return _Resp(
            {
                "results": [
                    {"id": "777", "title": "test-Green 360 - 25/08/2026"},
                    {"id": "888", "title": "Green 360 - 19/08/2026", "_links": {"webui": "/spaces/X/pages/888"}},
                ]
            }
        )

    monkeypatch.setattr(conf.requests, "get", fake_get)

    prev_data, prev_url = fetch_previous_360(("u", "p"), "https://example.atlassian.net/wiki", 42)

    # Anti-tautological: 777 is the most recent; only a correct "test" filter returns 888.
    assert prev_data == {"id": "888", "title": "Green 360 - 19/08/2026"}
    assert "888" in prev_url
    assert captured["url"].endswith("/rest/api/content/search")
    cql = captured["params"]["cql"]
    assert "parent=42" in cql
    assert "type=page" in cql
    assert "ORDER BY created DESC" in cql
