from config import _extract_folder_id, _extract_space_key, _field


def test_field_bold():
    assert _field("**Team:** Falcons", "Team") == "Falcons"


def test_field_bold_with_backticks():
    assert _field("**Space:** `TEST`", "Space") == "TEST"


def test_field_plain():
    assert _field("Team: Falcons", "Team") == "Falcons"


def test_field_missing():
    assert _field("nothing here", "Team") is None


def test_extract_space_key():
    assert _extract_space_key("https://example.atlassian.net/wiki/spaces/TEST/overview") == "TEST"


def test_extract_space_key_none():
    # Lowercase key does not match the [A-Z]+ pattern.
    assert _extract_space_key("https://example.atlassian.net/wiki/spaces/test") == ""


def test_extract_folder_id_folder():
    assert _extract_folder_id("https://example.atlassian.net/wiki/spaces/TEST/folder/12345") == "12345"


def test_extract_folder_id_pages():
    assert _extract_folder_id("https://example.atlassian.net/wiki/spaces/TEST/pages/67890/Title") == "67890"


def test_extract_folder_id_folder_wins_over_pages():
    url = "https://example.atlassian.net/wiki/folder/111/pages/222"
    assert _extract_folder_id(url) == "111"


def test_extract_folder_id_none():
    assert _extract_folder_id("https://example.atlassian.net/wiki/spaces/TEST") == ""
