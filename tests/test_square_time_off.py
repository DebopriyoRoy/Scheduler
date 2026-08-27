"""Parsing Square's Time off page.

LIVE_ROWS is verbatim output from the real dashboard on 27 Aug 2026, so the parser
is pinned against the shapes Square actually produces rather than invented ones.
"""

from datetime import date

import pytest
from django.core.management import call_command

from scheduling.integrations.square_time_off.normalizer import (
    parse_date_range,
    parse_row,
    parse_rows,
)
from scheduling.integrations.square_time_off.service import sync_time_off
from scheduling.models import Employee, EmployeeTimeOff, TimeOffSource, TimeOffStatus

LIVE_ROWS = [
    [
        "KG", "Kate Griffin", "Aug 27–Sep 2, 2026",
        "All day", "7d", "Out of town",
        "Approved", "7d", "0.00h",
    ],
    [
        "MR", "Molly Rittwage", "Aug 27–Sep 13, 2026",
        "All day", "18d", "Vacation",
        "Approved", "18d", "0.00h",
    ],
    [
        "KZ", "Khrystyna Zavadetska", "Aug 30–Sep 2, 2026",
        "All day", "4d", "Vacation off.",
        "Approved", "4d", "0.00h",
    ],
    [
        "YP", "Yana Pasechniuk", "Aug 31–Sep 2, 2026",
        "All day", "3d", "vacation",
        "Approved", "3d", "0.00h",
    ],
    ["JH", "John Harris", "Sep 1, 2026", "All day", "1d", "Test", "Declined", "0d", "0.00h"],
    [
        "DS", "Deborah Sweetapple", "Sep 3–4, 2026",
        "All day", "2d", "Out of province",
        "Approved", "2d", "0.00h",
    ],
    [
        "KG", "Kate Griffin", "Sep 22–27, 2026",
        "All day", "6d", "Birthday celebrations",
        "Declined", "7d", "0.00h",
    ],
    [
        "KG", "Kate Griffin", "Sep 25–27, 2026",
        "All day", "3d", "family in from out of town. staycation.",
        "Requested", "7d", "0.00h",
    ],
]


def test_every_live_row_parses():
    assert len(parse_rows(LIVE_ROWS)) == len(LIVE_ROWS)


def test_a_range_that_crosses_a_month_keeps_both_months():
    row = parse_row(LIVE_ROWS[0])
    assert row.start_date == date(2026, 8, 27)
    assert row.end_date == date(2026, 9, 2)


def test_a_same_month_range_repeats_the_month():
    """Square prints "Sep 3-4, 2026" - the second month is implied, not missing."""
    row = parse_row(LIVE_ROWS[5])
    assert row.start_date == date(2026, 9, 3)
    assert row.end_date == date(2026, 9, 4)


def test_a_single_day_has_the_same_start_and_end():
    row = parse_row(LIVE_ROWS[4])
    assert row.start_date == row.end_date == date(2026, 9, 1)


def test_a_range_crossing_new_year_starts_in_the_previous_year():
    """The printed year belongs to the end of the range."""
    assert parse_date_range("Dec 30–Jan 2, 2027") == (date(2026, 12, 30), date(2027, 1, 2))


def test_square_requested_is_stored_as_pending():
    """The word differs; the meaning is "not yet decided"."""
    assert parse_row(LIVE_ROWS[7]).status == TimeOffStatus.PENDING


@pytest.mark.parametrize(
    ("index", "status"),
    [(0, TimeOffStatus.APPROVED), (4, TimeOffStatus.DECLINED), (7, TimeOffStatus.PENDING)],
)
def test_statuses_map_across(index, status):
    assert parse_row(LIVE_ROWS[index]).status == status


def test_the_reason_survives_intact():
    assert parse_row(LIVE_ROWS[7]).reason == "family in from out of town. staycation."
    assert parse_row(LIVE_ROWS[2]).reason == "Vacation off."


def test_the_avatar_initials_are_not_mistaken_for_a_name():
    assert parse_row(LIVE_ROWS[0]).employee_name == "Kate Griffin"


def test_the_requested_and_approved_columns_are_kept():
    row = parse_row(LIVE_ROWS[1])
    assert row.requested_time == "18d"
    assert row.approved_all_day == "18d"
    assert row.approved_partial == "0.00h"


def test_a_row_with_no_reason_does_not_shift_the_status_into_it():
    """Reading positionally would take "Approved" as the reason and lose the status."""
    row = parse_row(
        ["KG", "Kate Griffin", "Sep 1, 2026", "All day", "1d", "Approved", "1d", "0.00h"]
    )
    assert row.status == TimeOffStatus.APPROVED
    assert row.reason == ""


def test_a_partial_day_row_is_not_marked_all_day():
    row = parse_row(
        [
            "JH", "John Harris", "Sep 1, 2026",
            "9:00am–12:00pm", "3h", "Appointment",
            "Approved", "0d", "3.00h",
        ]
    )
    assert row.all_day is False
    assert row.reason == "Appointment"


def test_a_header_or_junk_row_is_ignored():
    assert parse_row(["Team member", "Time off", "Requested time", "Reason", "Status"]) is None
    assert parse_row([]) is None


@pytest.mark.django_db
def test_sync_writes_the_live_rows_against_real_staff():
    call_command("seed_spirit_staff", verbosity=0)
    Employee.objects.update_or_create(
        display_name="Kate Griffin", defaults={"first_name": "Kate", "active": True}
    )
    Employee.objects.update_or_create(
        display_name="Molly Rittwage", defaults={"first_name": "Molly", "active": True}
    )

    result = sync_time_off(parse_rows(LIVE_ROWS))

    assert result.rows_seen == 8
    assert result.created >= 2
    kate = EmployeeTimeOff.objects.filter(employee__display_name="Kate Griffin")
    assert kate.filter(status=TimeOffStatus.APPROVED, start_date=date(2026, 8, 27)).exists()
    assert kate.filter(status=TimeOffStatus.PENDING, start_date=date(2026, 9, 25)).exists()


@pytest.mark.django_db
def test_a_resync_updates_rather_than_duplicating():
    Employee.objects.create(display_name="Kate Griffin", first_name="Kate", active=True)
    rows = parse_rows([LIVE_ROWS[0]])

    sync_time_off(rows)
    first_count = EmployeeTimeOff.objects.count()
    second = sync_time_off(rows)

    assert EmployeeTimeOff.objects.count() == first_count
    assert second.unchanged == 1


@pytest.mark.django_db
def test_a_request_withdrawn_in_square_stops_blocking_here():
    Employee.objects.create(display_name="Kate Griffin", first_name="Kate", active=True)
    sync_time_off(parse_rows([LIVE_ROWS[0]]))
    assert EmployeeTimeOff.objects.filter(source=TimeOffSource.SQUARE).exists()

    sync_time_off([])

    assert not EmployeeTimeOff.objects.filter(source=TimeOffSource.SQUARE).exists()


@pytest.mark.django_db
def test_a_hand_entered_absence_is_never_removed_by_a_sync():
    """Somebody typed it in deliberately; Square has no opinion about it."""
    employee = Employee.objects.create(display_name="Kate Griffin", first_name="Kate", active=True)
    EmployeeTimeOff.objects.create(
        employee=employee,
        start_date=date(2026, 11, 1),
        end_date=date(2026, 11, 2),
        status=TimeOffStatus.APPROVED,
        source=TimeOffSource.MANUAL,
    )

    sync_time_off([])

    assert EmployeeTimeOff.objects.filter(source=TimeOffSource.MANUAL).count() == 1


@pytest.mark.django_db
def test_an_unknown_name_is_reported_rather_than_silently_dropped():
    result = sync_time_off(parse_rows([LIVE_ROWS[0]]))
    assert "Kate Griffin" in result.unmatched
    assert "unmatched" in result.summary
