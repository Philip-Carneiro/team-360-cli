#!/usr/bin/env python3
"""Self-check for Confluence API v2 page creation. Run: python3 test_v2_migration.py"""

import publishers.confluence as conf


class MockResponse:
    def __init__(self, json_data):
        self._json = json_data

    def json(self):
        return self._json

    def raise_for_status(self):
        pass


calls = []


def mock_get(url, **kwargs):
    calls.append(("GET", url, kwargs))
    if "/spaces" in url:
        return MockResponse({"results": [{"id": "12345"}]})
    return MockResponse({})


def mock_post(url, **kwargs):
    calls.append(("POST", url, kwargs))
    return MockResponse({"id": "999", "_links": {"base": "https://foo.atlassian.net/wiki", "webui": "/pages/999"}})


conf.requests.get = mock_get
conf.requests.post = mock_post

# --- v2 create with space_key (base already includes /wiki) ---
calls.clear()
page_id, page_url = conf.publish_360(("u", "p"), "https://foo.atlassian.net/wiki", 100, "Test", "<p>x</p>", "MYSPACE")
assert len(calls) == 2, f"Expected 2 calls, got {len(calls)}"
assert calls[0][0] == "GET" and calls[0][1].endswith("/api/v2/spaces"), f"spaces lookup wrong: {calls[0][1]}"
assert "/rest/api/" not in calls[0][1], "v1 path leaked in spaces lookup"
assert calls[0][2].get("params") == {"keys": "MYSPACE"}, f"spaces params wrong: {calls[0][2].get('params')}"
assert calls[1][0] == "POST" and calls[1][1].endswith("/api/v2/pages"), f"create path wrong: {calls[1][1]}"
assert "/rest/api/" not in calls[1][1], "v1 path leaked in create"
body = calls[1][2]["json"]
assert body["spaceId"] == "12345", f"spaceId not resolved from lookup: {body.get('spaceId')}"
assert body["parentId"] == "100", f"parentId wrong: {body.get('parentId')}"
assert body["status"] == "current", f"status wrong: {body.get('status')}"
assert body["body"]["representation"] == "storage", f"representation wrong: {body['body']}"
assert page_id == 999, f"page_id wrong: {page_id}"
assert page_url == "https://foo.atlassian.net/wiki/pages/999", f"page_url wrong: {page_url}"

# --- base WITHOUT /wiki still produces a /wiki/api/v2 path (no doubling) ---
calls.clear()
conf.publish_360(("u", "p"), "https://foo.atlassian.net", 200, "T2", "<p>y</p>", "MYSPACE")
assert calls[0][1] == "https://foo.atlassian.net/wiki/api/v2/spaces", f"v2 base wrong: {calls[0][1]}"
assert calls[1][1] == "https://foo.atlassian.net/wiki/api/v2/pages", f"v2 base wrong: {calls[1][1]}"

# --- empty space_key raises (spec: spaceId is required for v2) ---
try:
    conf.publish_360(("u", "p"), "https://foo.atlassian.net/wiki", 300, "T3", "<p>z</p>", "")
    raise AssertionError("expected ValueError for empty space_key")
except ValueError:
    pass

print("v2 endpoints correct (/api/v2/spaces, /api/v2/pages), no /rest/api/")
print("spaceId resolved via params={'keys': ...}; parentId, status, body.representation=storage present")
print("v2 base tolerates url with and without /wiki")
print("empty space_key raises ValueError")

# --- fetch_previous_360 uses v1 CQL search and skips "test" pages ---
calls.clear()


def mock_get_search(url, **kwargs):
    calls.append(("GET", url, kwargs))
    if "/rest/api/content/search" in url:
        # Newest first: the test page must be skipped, the 19/08 page returned.
        return MockResponse(
            {
                "results": [
                    {"id": "777", "title": "test-Green 360 - 25/08/2026"},
                    {"id": "888", "title": "Green 360 - 19/08/2026", "_links": {"webui": "/spaces/X/pages/888"}},
                ]
            }
        )
    return MockResponse({})


conf.requests.get = mock_get_search

prev_data, prev_url = conf.fetch_previous_360(("u", "p"), "https://foo.atlassian.net/wiki", 42)

assert len(calls) == 1, f"Expected 1 call, got {len(calls)}"
assert calls[0][0] == "GET", f"Expected GET, got {calls[0][0]}"
assert calls[0][1].endswith("/rest/api/content/search"), f"Wrong endpoint: {calls[0][1]}"
assert "/api/v2/folders" not in calls[0][1] and "/folders/" not in calls[0][1], "v2 folders endpoint leaked"
cql = calls[0][2]["params"]["cql"]
assert "parent=42" in cql, f"CQL missing parent: {cql}"
assert "type=page" in cql, f"CQL missing type=page: {cql}"
assert "ORDER BY created DESC" in cql, f"CQL missing ordering: {cql}"
assert prev_data is not None, "prev_data should not be None"
# Anti-tautological: the test page (777) is newest; correct filter returns the 19/08 page (888).
assert prev_data["id"] == "888", f"Wrong page selected (test page not skipped?): {prev_data}"
assert prev_data["title"] == "Green 360 - 19/08/2026", f"Wrong title: {prev_data}"
assert "888" in prev_url, f"page_url wrong: {prev_url}"

print("fetch_previous_360 uses v1 /rest/api/content/search with CQL parent/type/ORDER BY")
print("fetch_previous_360 skips 'test' pages and returns the correct previous 360")
print("self-check OK")
