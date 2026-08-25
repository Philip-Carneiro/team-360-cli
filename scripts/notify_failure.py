import json
import os
import sys
import urllib.request


def build_message():
    workflow = os.environ.get("GITHUB_WORKFLOW", "unknown workflow")
    repo = os.environ.get("GITHUB_REPOSITORY", "unknown repo")
    run_id = os.environ.get("GITHUB_RUN_ID", "")
    server = os.environ.get("GITHUB_SERVER_URL", "https://github.com")
    job = os.environ.get("GITHUB_JOB", "unknown job")
    ref = os.environ.get("GITHUB_REF_NAME", "")
    sha = os.environ.get("GITHUB_SHA", "")

    run_url = f"{server}/{repo}/actions/runs/{run_id}"
    lines = [f":rotating_light: Workflow FAILED: {workflow} in {repo} (job {job}) — {run_url}"]
    if ref or sha:
        lines.append(f"Branch: {ref} | Commit: {sha[:8]}")

    try:
        with open("preflight-failure.txt") as f:
            detail = f.read().strip()
        if detail:
            lines.append(f"Preflight failures:\n{detail}")
    except FileNotFoundError:
        pass

    return "\n".join(lines)


def main():
    webhook = os.environ.get("SLACK_WEBHOOK_URL_ALERTS", "")
    if not webhook.strip():
        print("SLACK_WEBHOOK_URL_ALERTS not set — skipping failure alert")
        sys.exit(0)

    message = build_message()
    payload = json.dumps({"text": message}).encode()
    req = urllib.request.Request(webhook, data=payload, headers={"Content-Type": "application/json"})
    try:
        urllib.request.urlopen(req, timeout=20)
        print("Failure alert posted to Slack")
    except Exception as e:
        print(f"Failed to post failure alert: {e}")
    sys.exit(0)


if __name__ == "__main__":
    main()
