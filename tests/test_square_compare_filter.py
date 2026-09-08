"""Narrowing the Square comparison to one show date."""

import datetime as dt

from scheduling.services.square_reconcile import (
    ADDED_IN_SQUARE,
    EDITED_IN_SQUARE,
    REMOVED_FROM_SQUARE,
    ReconcileReport,
    ShiftDifference,
)

TEN = dt.date(2026, 9, 10)
ELEVEN = dt.date(2026, 9, 11)


def _report():
    report = ReconcileReport(schedule_run=None)
    report.differences = [
        ShiftDifference(EDITED_IN_SQUARE, TEN, None, "Joleen Dickson"),
        ShiftDifference(EDITED_IN_SQUARE, TEN, None, "Neil Bobbitt"),
        ShiftDifference(EDITED_IN_SQUARE, ELEVEN, None, "Joleen Dickson"),
        ShiftDifference(REMOVED_FROM_SQUARE, ELEVEN, None, "Olena Martynova"),
        ShiftDifference(ADDED_IN_SQUARE, ELEVEN, None, "Kate Griffin"),
    ]
    report.matched = 13
    report.matched_by_date = {TEN: 7, ELEVEN: 6}
    report.published_count = 33
    report.published_by_date = {TEN: 20, ELEVEN: 13}
    return report


def test_one_date_shows_only_that_date():
    report = _report()
    assert [d.employee_name for d in report.of_kind(EDITED_IN_SQUARE, TEN)] == [
        "Joleen Dickson",
        "Neil Bobbitt",
    ]
    assert report.of_kind(REMOVED_FROM_SQUARE, TEN) == []
    assert len(report.of_kind(ADDED_IN_SQUARE, ELEVEN)) == 1


def test_the_counts_follow_the_filter():
    """Period totals beside one night's table would describe dates that are not shown."""
    report = _report()
    assert report.matched_on(TEN) == 7
    assert report.published_on(TEN) == 20
    assert report.matched_on(ELEVEN) == 6
    # No date chosen still means the whole period.
    assert report.matched_on() == 13
    assert report.published_on() == 33
    # A date with nothing on it reads as zero, not as the total.
    assert report.matched_on(dt.date(2026, 9, 30)) == 0


def test_the_picker_offers_every_date_the_comparison_covers():
    report = _report()
    assert report.dates == [TEN, ELEVEN]


def test_no_date_chosen_is_unchanged_behaviour():
    report = _report()
    assert len(report.of_kind(EDITED_IN_SQUARE)) == 3
    assert len(report.of_kind(EDITED_IN_SQUARE, None)) == 3
