import json
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

from team_prs import (
    _collect_github_prs,
    _dedupe_prs,
    _human_review_decision,
    _is_bot,
    _match_to_roster,
    _review_status,
    _verify_open_prs,
)

ROSTER = [{"name": "Alice Smith"}, {"name": "Bob Jones"}]


def test_review_status_approved():
    assert _review_status("APPROVED") == "Approved"


def test_review_status_changes_requested():
    assert _review_status("CHANGES_REQUESTED") == "Changes Requested"


def test_review_status_waiting_variants():
    assert _review_status("REVIEW_REQUIRED") == "Waiting for Review"
    assert _review_status("") == "Waiting for Review"
    assert _review_status(None) == "Waiting for Review"


def test_match_substring():
    assert _match_to_roster("alice", ROSTER) == "Alice Smith"


def test_match_first_name_branch():
    # Full name is not a substring either way; only the first-name prefix rule can match.
    assert _match_to_roster("bob in accounting", ROSTER) == "Bob Jones"


def test_match_none():
    assert _match_to_roster("charlie", ROSTER) is None
    assert _match_to_roster("", ROSTER) is None


def test_dedupe_keeps_older_and_propagates_jira_key():
    url = "https://github.com/acme/repo/pull/1"
    prs = [
        {"url": url, "age_days": 2, "jira_key": "PROJ-1"},
        {"url": url, "age_days": 5, "jira_key": None},
    ]
    result = _dedupe_prs(prs)
    assert len(result) == 1
    # Older PR (larger age_days) is kept, jira_key propagated from the earlier entry.
    assert result[0]["age_days"] == 5
    assert result[0]["jira_key"] == "PROJ-1"


def test_dedupe_distinct_urls_kept():
    prs = [
        {"url": "https://github.com/acme/repo/pull/1", "age_days": 1},
        {"url": "https://github.com/acme/repo/pull/2", "age_days": 1},
    ]
    assert len(_dedupe_prs(prs)) == 2


# --- Tests for PR filtering (OPEN, non-draft only) ---


def test_collect_github_prs_filters_drafts():
    """Draft PRs from gh pr list are excluded. Non-drafts are kept."""
    gh_output = json.dumps(
        [
            {
                "number": 1,
                "title": "Non-draft PR",
                "author": {"login": "alice"},
                "createdAt": "2026-08-20T10:00:00Z",
                "url": "https://github.com/acme/repo/pull/1",
                "reviewDecision": "APPROVED",
                "isDraft": False,
            },
            {
                "number": 2,
                "title": "Draft PR",
                "author": {"login": "bob"},
                "createdAt": "2026-08-21T10:00:00Z",
                "url": "https://github.com/acme/repo/pull/2",
                "reviewDecision": "",
                "isDraft": True,
            },
        ]
    )

    mock_proc = MagicMock()
    mock_proc.returncode = 0
    mock_proc.stdout = gh_output

    with patch("subprocess.run", return_value=mock_proc):
        result = _collect_github_prs(["acme/repo"], [])

    assert len(result) == 1
    assert result[0]["number"] == 1
    assert result[0]["title"] == "Non-draft PR"


def test_collect_github_prs_empty_when_all_drafts():
    """If all PRs are drafts, result is empty."""
    gh_output = json.dumps(
        [
            {
                "number": 1,
                "title": "Draft 1",
                "author": {"login": "alice"},
                "createdAt": "2026-08-20T10:00:00Z",
                "url": "https://github.com/acme/repo/pull/1",
                "reviewDecision": "",
                "isDraft": True,
            },
            {
                "number": 2,
                "title": "Draft 2",
                "author": {"login": "bob"},
                "createdAt": "2026-08-21T10:00:00Z",
                "url": "https://github.com/acme/repo/pull/2",
                "reviewDecision": "",
                "isDraft": True,
            },
        ]
    )

    mock_proc = MagicMock()
    mock_proc.returncode = 0
    mock_proc.stdout = gh_output

    with patch("subprocess.run", return_value=mock_proc):
        result = _collect_github_prs(["acme/repo"], [])

    assert len(result) == 0


# --- Tests for jira-sourced PR enrichment and filtering ---


def _mock_config():
    """Minimal config for testing run()."""
    from types import SimpleNamespace

    return SimpleNamespace(
        team_name="Test Team",
        roster=[SimpleNamespace(name="Alice Smith", role="Dev"), SimpleNamespace(name="Bob Jones", role="Dev")],
        repo_mapping={"upstream": "https://github.com/acme/repo"},
        jira_label="test-label",
        jira_components=["test-component"],
    )


def _mock_env():
    """Minimal env vars for testing run()."""
    return {
        "JIRA_EMAIL": "test@example.com",
        "JIRA_API_TOKEN": "fake-token",
        "JIRA_BASE_URL": "https://example.com",
    }


def _capture_prs_factory():
    """Factory that returns a mock function and a list to capture PRs."""
    captured = []

    def mock_generate_report(team_name, roster, prs):
        captured.extend(prs)
        return "# Mock Report"

    return mock_generate_report, captured


def test_jira_sourced_closed_pr_removed(monkeypatch, tmp_path):
    """JIRA-sourced PR with state=CLOSED is removed from final report."""
    for k, v in _mock_env().items():
        monkeypatch.setenv(k, v)

    mock_report_fn, captured_prs = _capture_prs_factory()

    with (
        patch("config.load_config", return_value=_mock_config()),
        patch("team_prs._collect_github_prs", return_value=[]),
        patch(
            "team_prs._collect_jira_prs",
            return_value=[
                {
                    "repo": "acme/repo",
                    "number": 1,
                    "title": "Jira PR",
                    "author": "Alice Smith",
                    "url": "https://github.com/acme/repo/pull/1",
                    "age_days": 0,
                    "review_decision": "",
                    "platform": "github",
                    "jira_key": "PROJ-1",
                }
            ],
        ),
        patch("subprocess.run") as mock_run,
        patch("team_prs._generate_report", side_effect=mock_report_fn),
        patch("team_prs._SCRIPT_DIR", tmp_path),
        patch("team_prs._load_teams", return_value={"Test Team": {}}),
        patch("team_prs._select_team", return_value="Test Team"),
    ):
        mock_proc_view = MagicMock()
        mock_proc_view.returncode = 0
        mock_proc_view.stdout = json.dumps(
            {"state": "CLOSED", "isDraft": False, "createdAt": "2026-08-20T10:00:00Z", "reviewDecision": ""}
        )
        mock_run.return_value = mock_proc_view

        from team_prs import run

        run(team_arg="Test")

        assert len(captured_prs) == 0


def test_jira_sourced_unverifiable_pr_removed(monkeypatch, tmp_path):
    """JIRA-sourced PR that fails verification (returncode != 0) is removed."""
    for k, v in _mock_env().items():
        monkeypatch.setenv(k, v)

    mock_report_fn, captured_prs = _capture_prs_factory()

    with (
        patch("config.load_config", return_value=_mock_config()),
        patch("team_prs._collect_github_prs", return_value=[]),
        patch(
            "team_prs._collect_jira_prs",
            return_value=[
                {
                    "repo": "acme/repo",
                    "number": 2,
                    "title": "Private Repo PR",
                    "author": "Bob Jones",
                    "url": "https://github.com/acme/private/pull/2",
                    "age_days": 0,
                    "review_decision": "",
                    "platform": "github",
                    "jira_key": "PROJ-2",
                }
            ],
        ),
        patch("subprocess.run") as mock_run,
        patch("team_prs._generate_report", side_effect=mock_report_fn),
        patch("team_prs._SCRIPT_DIR", tmp_path),
        patch("team_prs._load_teams", return_value={"Test Team": {}}),
        patch("team_prs._select_team", return_value="Test Team"),
    ):
        mock_proc_view = MagicMock()
        mock_proc_view.returncode = 1
        mock_run.return_value = mock_proc_view

        from team_prs import run

        run(team_arg="Test")

        assert len(captured_prs) == 0


def test_jira_sourced_exception_pr_removed(monkeypatch, tmp_path):
    """JIRA-sourced PR that raises exception during verification is removed."""
    for k, v in _mock_env().items():
        monkeypatch.setenv(k, v)

    mock_report_fn, captured_prs = _capture_prs_factory()

    with (
        patch("config.load_config", return_value=_mock_config()),
        patch("team_prs._collect_github_prs", return_value=[]),
        patch(
            "team_prs._collect_jira_prs",
            return_value=[
                {
                    "repo": "acme/repo",
                    "number": 3,
                    "title": "Exception PR",
                    "author": "Alice Smith",
                    "url": "https://github.com/acme/repo/pull/3",
                    "age_days": 0,
                    "review_decision": "",
                    "platform": "github",
                    "jira_key": "PROJ-3",
                }
            ],
        ),
        patch("subprocess.run", side_effect=json.JSONDecodeError("Invalid", "", 0)),
        patch("team_prs._generate_report", side_effect=mock_report_fn),
        patch("team_prs._SCRIPT_DIR", tmp_path),
        patch("team_prs._load_teams", return_value={"Test Team": {}}),
        patch("team_prs._select_team", return_value="Test Team"),
    ):
        from team_prs import run

        run(team_arg="Test")

        assert len(captured_prs) == 0


def test_jira_sourced_draft_pr_removed(monkeypatch, tmp_path):
    """JIRA-sourced PR with isDraft=true is removed."""
    for k, v in _mock_env().items():
        monkeypatch.setenv(k, v)

    mock_report_fn, captured_prs = _capture_prs_factory()

    with (
        patch("config.load_config", return_value=_mock_config()),
        patch("team_prs._collect_github_prs", return_value=[]),
        patch(
            "team_prs._collect_jira_prs",
            return_value=[
                {
                    "repo": "acme/repo",
                    "number": 4,
                    "title": "Draft Jira PR",
                    "author": "Bob Jones",
                    "url": "https://github.com/acme/repo/pull/4",
                    "age_days": 0,
                    "review_decision": "",
                    "platform": "github",
                    "jira_key": "PROJ-4",
                }
            ],
        ),
        patch("subprocess.run") as mock_run,
        patch("team_prs._generate_report", side_effect=mock_report_fn),
        patch("team_prs._SCRIPT_DIR", tmp_path),
        patch("team_prs._load_teams", return_value={"Test Team": {}}),
        patch("team_prs._select_team", return_value="Test Team"),
    ):
        mock_proc_view = MagicMock()
        mock_proc_view.returncode = 0
        mock_proc_view.stdout = json.dumps(
            {"state": "OPEN", "isDraft": True, "createdAt": "2026-08-20T10:00:00Z", "reviewDecision": ""}
        )
        mock_run.return_value = mock_proc_view

        from team_prs import run

        run(team_arg="Test")

        assert len(captured_prs) == 0


def test_jira_sourced_open_nondraft_kept_with_correct_age(monkeypatch, tmp_path):
    """JIRA-sourced PR with state=OPEN and isDraft=false is kept with correct age."""
    for k, v in _mock_env().items():
        monkeypatch.setenv(k, v)

    five_days_ago = (datetime.now(timezone.utc) - timedelta(days=5)).isoformat().replace("+00:00", "Z")
    mock_report_fn, captured_prs = _capture_prs_factory()

    with (
        patch("config.load_config", return_value=_mock_config()),
        patch("team_prs._collect_github_prs", return_value=[]),
        patch(
            "team_prs._collect_jira_prs",
            return_value=[
                {
                    "repo": "acme/repo",
                    "number": 5,
                    "title": "Open PR",
                    "author": "Alice Smith",
                    "url": "https://github.com/acme/repo/pull/5",
                    "age_days": 0,
                    "review_decision": "",
                    "platform": "github",
                    "jira_key": "PROJ-5",
                }
            ],
        ),
        patch("subprocess.run") as mock_run,
        patch("team_prs._generate_report", side_effect=mock_report_fn),
        patch("team_prs._SCRIPT_DIR", tmp_path),
        patch("team_prs._load_teams", return_value={"Test Team": {}}),
        patch("team_prs._select_team", return_value="Test Team"),
    ):
        mock_proc_view = MagicMock()
        mock_proc_view.returncode = 0
        mock_proc_view.stdout = json.dumps(
            {
                "state": "OPEN",
                "isDraft": False,
                "createdAt": five_days_ago,
                "reviews": [{"author": {"login": "human-reviewer"}, "state": "APPROVED"}],
            }
        )
        mock_run.return_value = mock_proc_view

        from team_prs import run

        run(team_arg="Test")

        assert len(captured_prs) == 1
        assert captured_prs[0]["url"] == "https://github.com/acme/repo/pull/5"
        assert captured_prs[0]["age_days"] >= 4
        assert captured_prs[0]["age_days"] <= 6
        assert captured_prs[0]["review_decision"] == "APPROVED"


def test_dedup_before_verification_url_verified_once(monkeypatch, tmp_path):
    """Same URL from github+jira sources is deduplicated BEFORE verification, so gh pr view runs at most once per URL."""
    for k, v in _mock_env().items():
        monkeypatch.setenv(k, v)

    url = "https://github.com/acme/repo/pull/6"
    three_days_ago = (datetime.now(timezone.utc) - timedelta(days=3)).isoformat().replace("+00:00", "Z")
    mock_report_fn, captured_prs = _capture_prs_factory()

    with (
        patch("config.load_config", return_value=_mock_config()),
        patch(
            "team_prs._collect_github_prs",
            return_value=[
                {
                    "repo": "acme/repo",
                    "number": 6,
                    "title": "GitHub PR",
                    "author": "alice",
                    "url": url,
                    "age_days": 3,
                    "review_decision": "",
                    "platform": "github",
                }
            ],
        ),
        patch(
            "team_prs._collect_jira_prs",
            return_value=[
                {
                    "repo": "acme/repo",
                    "number": 6,
                    "title": "Jira PR",
                    "author": "Alice Smith",
                    "url": url,
                    "age_days": 0,
                    "review_decision": "",
                    "platform": "github",
                    "jira_key": "PROJ-6",
                }
            ],
        ),
        patch("subprocess.run") as mock_run,
        patch("team_prs._generate_report", side_effect=mock_report_fn),
        patch("team_prs._SCRIPT_DIR", tmp_path),
        patch("team_prs._load_teams", return_value={"Test Team": {}}),
        patch("team_prs._select_team", return_value="Test Team"),
    ):
        mock_proc_view = MagicMock()
        mock_proc_view.returncode = 0
        mock_proc_view.stdout = json.dumps(
            {"state": "OPEN", "isDraft": False, "createdAt": three_days_ago, "reviewDecision": ""}
        )
        mock_run.return_value = mock_proc_view

        from team_prs import run

        run(team_arg="Test")

        # Count how many times gh pr view was called for this URL
        view_calls = [
            c
            for c in mock_run.call_args_list
            if "gh" in c[0][0] and "pr" in c[0][0] and "view" in c[0][0] and url in c[0][0]
        ]
        assert (
            len(view_calls) == 0
        ), "gh pr view should not be called for github-sourced PR with age > 0 (only jira-sourced with age==0 are enriched)"

        assert len(captured_prs) == 1


# --- Tests for _verify_open_prs helper (direct calls, bug #636 coverage) ---


def test_verify_open_prs_github_closed_dropped():
    """GitHub PR with state=CLOSED is dropped."""
    pr = {
        "url": "https://github.com/acme/repo/pull/1",
        "platform": "github",
        "age_days": 0,
    }
    mock_proc = MagicMock()
    mock_proc.returncode = 0
    mock_proc.stdout = json.dumps({"state": "CLOSED", "isDraft": False, "createdAt": "2026-08-20T10:00:00Z"})

    with patch("subprocess.run", return_value=mock_proc):
        drop_urls = _verify_open_prs([pr])

    assert pr["url"] in drop_urls
    assert len(drop_urls) == 1


def test_verify_open_prs_github_returncode_nonzero_dropped():
    """GitHub PR with returncode != 0 is dropped (bug #636 fix)."""
    pr = {
        "url": "https://github.com/acme/repo/pull/2",
        "platform": "github",
        "age_days": 0,
    }
    mock_proc = MagicMock()
    mock_proc.returncode = 1

    with patch("subprocess.run", return_value=mock_proc):
        drop_urls = _verify_open_prs([pr])

    assert pr["url"] in drop_urls


def test_verify_open_prs_github_draft_dropped():
    """GitHub PR with isDraft=true is dropped."""
    pr = {
        "url": "https://github.com/acme/repo/pull/3",
        "platform": "github",
        "age_days": 0,
    }
    mock_proc = MagicMock()
    mock_proc.returncode = 0
    mock_proc.stdout = json.dumps({"state": "OPEN", "isDraft": True, "createdAt": "2026-08-20T10:00:00Z"})

    with patch("subprocess.run", return_value=mock_proc):
        drop_urls = _verify_open_prs([pr])

    assert pr["url"] in drop_urls


def test_verify_open_prs_github_open_nondraft_kept_and_mutated():
    """GitHub PR OPEN + non-draft is NOT dropped, age_days and review_decision are mutated."""
    five_days_ago = (datetime.now(timezone.utc) - timedelta(days=5)).isoformat().replace("+00:00", "Z")
    pr = {
        "url": "https://github.com/acme/repo/pull/4",
        "platform": "github",
        "age_days": 0,
        "review_decision": "",
    }
    mock_proc = MagicMock()
    mock_proc.returncode = 0
    mock_proc.stdout = json.dumps(
        {
            "state": "OPEN",
            "isDraft": False,
            "createdAt": five_days_ago,
            "reviews": [{"author": {"login": "human-reviewer"}, "state": "APPROVED"}],
        }
    )

    with patch("subprocess.run", return_value=mock_proc):
        drop_urls = _verify_open_prs([pr])

    assert pr["url"] not in drop_urls
    assert pr["age_days"] >= 4
    assert pr["age_days"] <= 6
    assert pr["review_decision"] == "APPROVED"


def test_verify_open_prs_unique_urls_verified_once():
    """Each unique URL is verified exactly once (subprocess.run called once per URL)."""
    url1 = "https://github.com/acme/repo/pull/5"
    url2 = "https://github.com/acme/repo/pull/6"
    prs = [
        {"url": url1, "platform": "github", "age_days": 0},
        {"url": url2, "platform": "github", "age_days": 0},
    ]
    three_days_ago = (datetime.now(timezone.utc) - timedelta(days=3)).isoformat().replace("+00:00", "Z")
    mock_proc = MagicMock()
    mock_proc.returncode = 0
    mock_proc.stdout = json.dumps(
        {"state": "OPEN", "isDraft": False, "createdAt": three_days_ago, "reviewDecision": ""}
    )

    with patch("subprocess.run", return_value=mock_proc) as mock_run:
        _verify_open_prs(prs)

    assert mock_run.call_count == 2


def test_verify_open_prs_gitlab_state_not_opened_dropped():
    """GitLab MR with state != opened is dropped."""
    pr = {
        "url": "https://gitlab.com/group/project/-/merge_requests/7",
        "platform": "gitlab",
        "age_days": 0,
    }
    mock_proc = MagicMock()
    mock_proc.returncode = 0
    mock_proc.stdout = json.dumps({"state": "merged", "draft": False, "work_in_progress": False})

    with patch("subprocess.run", return_value=mock_proc):
        drop_urls = _verify_open_prs([pr])

    assert pr["url"] in drop_urls


# --- Tests for _human_review_decision helper (bug #9229 coverage) ---


def test_human_review_decision_bot_only_changes_requested():
    """Bot-only CHANGES_REQUESTED is ignored -> returns empty string (bug #9229)."""
    reviews = [{"author": {"login": "coderabbitai[bot]"}, "state": "CHANGES_REQUESTED"}]
    assert _human_review_decision(reviews) == ""


def test_human_review_decision_human_changes_requested():
    """Human CHANGES_REQUESTED -> 'CHANGES_REQUESTED'."""
    reviews = [{"author": {"login": "alice"}, "state": "CHANGES_REQUESTED"}]
    assert _human_review_decision(reviews) == "CHANGES_REQUESTED"


def test_human_review_decision_human_approved():
    """Human APPROVED -> 'APPROVED'."""
    reviews = [{"author": {"login": "bob"}, "state": "APPROVED"}]
    assert _human_review_decision(reviews) == "APPROVED"


def test_human_review_decision_human_approved_bot_changes_requested():
    """Human APPROVED + bot CHANGES_REQUESTED -> 'APPROVED' (bot ignored)."""
    reviews = [
        {"author": {"login": "alice"}, "state": "APPROVED"},
        {"author": {"login": "dependabot[bot]"}, "state": "CHANGES_REQUESTED"},
    ]
    assert _human_review_decision(reviews) == "APPROVED"


def test_human_review_decision_divergent_humans():
    """Two humans divergent: one CHANGES_REQUESTED, one APPROVED -> 'CHANGES_REQUESTED'."""
    reviews = [
        {"author": {"login": "alice"}, "state": "CHANGES_REQUESTED"},
        {"author": {"login": "bob"}, "state": "APPROVED"},
    ]
    assert _human_review_decision(reviews) == "CHANGES_REQUESTED"


def test_human_review_decision_commented_or_empty():
    """Only COMMENTED review or empty list -> empty string."""
    assert _human_review_decision([{"author": {"login": "alice"}, "state": "COMMENTED"}]) == ""
    assert _human_review_decision([]) == ""


def test_verify_open_prs_bot_only_review_stays_waiting():
    """PR with only bot CHANGES_REQUESTED stays 'Waiting for Review' (not 'Changes Requested')."""
    pr = {
        "url": "https://github.com/acme/repo/pull/9229",
        "platform": "github",
        "age_days": 0,
        "review_decision": "",
    }
    three_days_ago = (datetime.now(timezone.utc) - timedelta(days=3)).isoformat().replace("+00:00", "Z")
    mock_proc = MagicMock()
    mock_proc.returncode = 0
    mock_proc.stdout = json.dumps(
        {
            "state": "OPEN",
            "isDraft": False,
            "createdAt": three_days_ago,
            "reviews": [{"author": {"login": "coderabbitai[bot]"}, "state": "CHANGES_REQUESTED"}],
        }
    )

    with patch("subprocess.run", return_value=mock_proc):
        drop_urls = _verify_open_prs([pr])

    assert pr["url"] not in drop_urls
    assert pr["review_decision"] == ""
    assert _review_status(pr["review_decision"]) == "Waiting for Review"


# --- Tests for _is_bot helper ---


def test_is_bot_with_bracket_suffix():
    """Bot with [bot] suffix is detected (case-insensitive)."""
    assert _is_bot("coderabbitai[bot]")
    assert _is_bot("dependabot[bot]")
    assert _is_bot("Renovate[Bot]")


def test_is_bot_known_login_without_suffix():
    """Bot in _KNOWN_BOT_LOGINS without [bot] suffix is detected (bug fix for #9229)."""
    assert _is_bot("coderabbitai")
    assert _is_bot("dependabot")
    assert _is_bot("odh-dashboard-agent")
    assert _is_bot("openshift-merge-bot")


def test_is_bot_human_login():
    """Human login is not detected as bot."""
    assert not _is_bot("alice")
    assert not _is_bot("bob-reviewer")
    assert not _is_bot("")


# --- Tests for _human_review_decision with commits (bug #9229 coverage) ---


def test_human_review_decision_cr_without_commit():
    """CR humano SEM commits -> 'CHANGES_REQUESTED'."""
    reviews = [{"author": {"login": "alice"}, "state": "CHANGES_REQUESTED", "submittedAt": "2020-01-01T00:00:00Z"}]
    commits = []
    assert _human_review_decision(reviews, commits) == "CHANGES_REQUESTED"


def test_human_review_decision_cr_with_commit_before():
    """CR humano com commit ANTES do CR -> 'CHANGES_REQUESTED'."""
    reviews = [{"author": {"login": "alice"}, "state": "CHANGES_REQUESTED", "submittedAt": "2020-01-01T00:00:00Z"}]
    commits = [{"committedDate": "2019-12-31T23:59:59Z"}]
    assert _human_review_decision(reviews, commits) == "CHANGES_REQUESTED"


def test_human_review_decision_cr_with_commit_after():
    """CR humano com commit DEPOIS do CR -> '' (Waiting for Review) — reproduz bug #9229."""
    reviews = [{"author": {"login": "alice"}, "state": "CHANGES_REQUESTED", "submittedAt": "2020-01-01T00:00:00Z"}]
    commits = [{"committedDate": "2020-01-02T00:00:00Z"}]
    assert _human_review_decision(reviews, commits) == ""


def test_human_review_decision_bot_coderabbitai_without_suffix():
    """Bot 'coderabbitai' SEM [bot] + outro bot COM [bot], sem humanos -> ''."""
    reviews = [
        {"author": {"login": "coderabbitai"}, "state": "CHANGES_REQUESTED", "submittedAt": "2020-01-01T00:00:00Z"},
        {"author": {"login": "dependabot[bot]"}, "state": "CHANGES_REQUESTED", "submittedAt": "2020-01-01T00:00:00Z"},
    ]
    assert _human_review_decision(reviews) == ""


def test_human_review_decision_two_humans_cr_and_approved_without_commit():
    """Dois humanos: CR (sem commit depois) + APPROVED -> 'CHANGES_REQUESTED'."""
    reviews = [
        {"author": {"login": "alice"}, "state": "CHANGES_REQUESTED", "submittedAt": "2020-01-01T00:00:00Z"},
        {"author": {"login": "bob"}, "state": "APPROVED", "submittedAt": "2020-01-01T12:00:00Z"},
    ]
    commits = []
    assert _human_review_decision(reviews, commits) == "CHANGES_REQUESTED"
