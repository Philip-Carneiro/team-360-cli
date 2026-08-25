import urllib.error

import scripts.preflight as preflight

JIRA_VARS = ("JIRA_BASE_URL", "JIRA_EMAIL", "JIRA_API_TOKEN")
GOOGLE_VARS = ("GOOGLE_CLIENT_ID", "GOOGLE_CLIENT_SECRET", "GOOGLE_REFRESH_TOKEN")


def test_teams_missing(monkeypatch):
    monkeypatch.delenv("TEAMS_JSON", raising=False)
    assert preflight.check_teams() == "TEAMS_JSON missing/empty"


def test_jira_missing(monkeypatch):
    for v in JIRA_VARS:
        monkeypatch.delenv(v, raising=False)
    assert preflight.check_jira() == "missing JIRA_BASE_URL, JIRA_EMAIL, JIRA_API_TOKEN"


def test_google_missing(monkeypatch):
    for v in GOOGLE_VARS:
        monkeypatch.delenv(v, raising=False)
    assert preflight.check_google() == "missing GOOGLE_CLIENT_ID, GOOGLE_CLIENT_SECRET, GOOGLE_REFRESH_TOKEN"


def test_jira_401_invalid_credentials(monkeypatch):
    monkeypatch.setenv("JIRA_BASE_URL", "https://example.atlassian.net")
    monkeypatch.setenv("JIRA_EMAIL", "user@example.com")
    monkeypatch.setenv("JIRA_API_TOKEN", "dummy-token")

    def fake_urlopen(*args, **kwargs):
        raise urllib.error.HTTPError("https://example.atlassian.net", 401, "Unauthorized", {}, None)

    monkeypatch.setattr(preflight.urllib.request, "urlopen", fake_urlopen)
    assert preflight.check_jira() == "HTTP 401 (invalid Jira/Confluence credentials)"
