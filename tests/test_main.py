from main import _swimlane_to_jql


def test_single_swimlane():
    jql, name = _swimlane_to_jql("1.0")
    assert jql == 'fixVersion = "1.0"'
    assert name == "1.0"


def test_multi_swimlane():
    jql, name = _swimlane_to_jql("1.0, 2.0")
    assert jql == 'fixVersion IN ("1.0", "2.0")'
    assert name == "1.0, 2.0"


def test_multi_swimlane_ignores_empty_parts():
    jql, name = _swimlane_to_jql("1.0, , 2.0")
    assert jql == 'fixVersion IN ("1.0", "2.0")'
    assert name == "1.0, 2.0"
