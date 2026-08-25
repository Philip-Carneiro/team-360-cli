import pytest

import scripts.notify_failure as nf


def test_build_message_includes_preflight_detail(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "preflight-failure.txt").write_text("FAIL jira: missing JIRA_API_TOKEN\n")
    msg = nf.build_message()
    assert "Preflight failures:" in msg
    assert "FAIL jira: missing JIRA_API_TOKEN" in msg


def test_build_message_no_detail_when_file_absent(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    msg = nf.build_message()
    assert "Preflight failures:" not in msg


def test_main_no_webhook_does_not_post(monkeypatch):
    monkeypatch.delenv("SLACK_WEBHOOK_URL_ALERTS", raising=False)

    called = {"hit": False}

    def spy(*args, **kwargs):
        called["hit"] = True

    monkeypatch.setattr(nf.urllib.request, "urlopen", spy)

    with pytest.raises(SystemExit) as exc:
        nf.main()
    assert exc.value.code == 0
    assert called["hit"] is False
