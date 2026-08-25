import base64
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request


def check_teams():
    raw = os.environ.get("TEAMS_JSON", "")
    if not raw.strip():
        return "TEAMS_JSON missing/empty"
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as e:
        return f"TEAMS_JSON invalid JSON: {e}"
    if not isinstance(data, dict) or not data:
        return "TEAMS_JSON must be a non-empty object"
    return None


def check_jira():
    base = os.environ.get("JIRA_BASE_URL", "").rstrip("/")
    email = os.environ.get("JIRA_EMAIL", "")
    token = os.environ.get("JIRA_API_TOKEN", "")
    missing = [n for n, v in [("JIRA_BASE_URL", base), ("JIRA_EMAIL", email), ("JIRA_API_TOKEN", token)] if not v]
    if missing:
        return f"missing {', '.join(missing)}"
    auth = base64.b64encode(f"{email}:{token}".encode()).decode()
    req = urllib.request.Request(
        f"{base}/rest/api/3/myself", headers={"Authorization": f"Basic {auth}", "Accept": "application/json"}
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return None if r.status == 200 else f"HTTP {r.status}"
    except urllib.error.HTTPError as e:
        return f"HTTP {e.code} (invalid Jira/Confluence credentials)" if e.code in (401, 403) else f"HTTP {e.code}"
    except Exception as e:
        return f"request failed: {e}"


def check_google():
    cid = os.environ.get("GOOGLE_CLIENT_ID", "")
    secret = os.environ.get("GOOGLE_CLIENT_SECRET", "")
    refresh = os.environ.get("GOOGLE_REFRESH_TOKEN", "")
    missing = [
        n
        for n, v in [("GOOGLE_CLIENT_ID", cid), ("GOOGLE_CLIENT_SECRET", secret), ("GOOGLE_REFRESH_TOKEN", refresh)]
        if not v
    ]
    if missing:
        return f"missing {', '.join(missing)}"
    data = urllib.parse.urlencode(
        {
            "client_id": cid,
            "client_secret": secret,
            "refresh_token": refresh,
            "grant_type": "refresh_token",
        }
    ).encode()
    req = urllib.request.Request("https://oauth2.googleapis.com/token", data=data)
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            body = json.loads(r.read().decode())
            return None if body.get("access_token") else "no access_token in response"
    except urllib.error.HTTPError as e:
        return f"HTTP {e.code} (invalid Google OAuth credentials)"
    except Exception as e:
        return f"request failed: {e}"


CHECKS = {"teams": check_teams, "jira": check_jira, "google": check_google}


def main():
    requested = [c.strip() for c in os.environ.get("PREFLIGHT_CHECKS", "").split(",") if c.strip()]
    failures = []
    for name in requested:
        fn = CHECKS.get(name)
        if not fn:
            print(f"  ? {name}: unknown check (skipped)")
            continue
        err = fn()
        if err:
            print(f"  FAIL {name}: {err}")
            failures.append(name)
        else:
            print(f"  OK   {name}")
    if failures:
        print(f"\nPreflight failed for: {', '.join(failures)}", file=sys.stderr)
        sys.exit(1)
    print("\nAll secrets OK")


if __name__ == "__main__":
    main()
