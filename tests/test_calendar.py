"""Regression tests for calendar.py bug fixes."""


def test_date_ordering_across_year_boundary():
    """FIX 5 regression: dates crossing Dec→Jan must sort chronologically.

    Before fix: sorted by datetime.strptime("%d/%m") with year=1900 → 30/12 > 02/01.
    After fix: sort by real datetime preserving year context → 30/12 < 01/01.
    """
    from collectors.calendar import _parse_event_dates

    # Event spanning Dec 30 → Jan 2 (4 days: 30/12, 31/12, 01/01, 02/01)
    event = {
        "start": {"date": "2025-12-30"},
        "end": {"date": "2026-01-03"},  # exclusive end
    }

    result = _parse_event_dates(event)

    # Result is list of (display_str, sort_datetime) tuples
    assert len(result) == 4
    display_dates = [t[0] for t in result]
    sort_dates = [t[1] for t in result]

    # Display format is DD/MM
    assert display_dates == ["30/12", "31/12", "01/01", "02/01"]

    # Sort dates must be chronologically ordered
    assert sort_dates == sorted(sort_dates)
    assert sort_dates[0] < sort_dates[1] < sort_dates[2] < sort_dates[3]

    # Specifically: Dec dates come before Jan dates
    assert sort_dates[0].month == 12
    assert sort_dates[2].month == 1
    assert sort_dates[0] < sort_dates[2]
