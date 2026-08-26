"""The roster page must show what Square currently holds, and nothing else.

Seventeen staff appeared on this page as free "All day" every Monday and Sunday, and
Jackie Pynn - who Square says does not work Sunday, Monday or Tuesday - was shown as
available on two of them. The rows behind it came from a provider that no longer
exists, and they won because the page ranked rows by the availability *date* rather
than by when the row was written: the retired feed had written rows dated September,
the live sync writes rows through December, so a months-old value outranked a
minutes-old one whenever its date happened to fall later.
"""

from datetime import date, timedelta

import pytest
from django.contrib.auth import get_user_model
from django.urls import reverse

from scheduling.integrations.square_availability.service import (
    FALLBACK_AVAILABILITY_SOURCE,
    SQUARE_AVAILABILITY_SOURCE,
)
from scheduling.models import AvailabilityType, Employee, EmployeeAvailability

RETIRED = "LIVE_SQUARE_AVAILABILITY"


@pytest.fixture
def manager(db):
    return get_user_model().objects.create_user(username="mgr", password="safe-test-password")


@pytest.fixture
def jackie(db):
    return Employee.objects.create(display_name="Jackie Pynn", first_name="Jackie", active=True)


def _next_weekday(weekday: int, after: date) -> date:
    step = (weekday - after.weekday()) % 7 or 7
    return after + timedelta(days=step)


WEEKDAY_COLUMNS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")


def _cell(html: str, employee_name: str, weekday: str) -> str:
    """The text of one weekday cell on a person's rendered roster row.

    Addressed by weekday name rather than a column number: the row also carries the
    name, the roles, the Square mapping and the status, and an off-by-one against
    those reads a neighbouring column while still looking like a real answer.
    """
    import html as html_module
    import re

    body = html.split("<tbody>", 1)[1].split("</tbody>", 1)[0]
    for row in re.findall(r"<tr>(.*?)</tr>", body, re.S):
        if employee_name not in row:
            continue
        cells = [
            " ".join(html_module.unescape(re.sub(r"<[^>]+>", " ", cell)).split())
            for cell in re.findall(r"<td.*?>(.*?)</td>", row, re.S)
        ]
        # name, roles, then one cell per weekday.
        return cells[2 + WEEKDAY_COLUMNS.index(weekday)]
    raise AssertionError(f"no row for {employee_name}")


def test_retired_source_never_shows_hours(client, manager, jackie):
    """A row from a provider that no longer exists must not appear at all.

    This is the exact shape of the live bug: the retired row's date is *later* than
    the current one, so any ranking that sorts by date puts it on top.
    """
    monday = _next_weekday(0, date.today())
    EmployeeAvailability.objects.create(
        employee=jackie,
        date=monday + timedelta(days=70),
        availability_type=AvailabilityType.AVAILABLE_ALL_DAY,
        source=RETIRED,
    )
    EmployeeAvailability.objects.create(
        employee=jackie,
        date=monday,
        availability_type=AvailabilityType.UNKNOWN,
        source=SQUARE_AVAILABILITY_SOURCE,
    )

    client.force_login(manager)
    html = client.get(reverse("employees")).content.decode()

    assert _cell(html, "Jackie Pynn", "Mon") == "—"
    assert "All day" not in html


def test_square_read_outranks_the_fixture_stand_in(client, manager, jackie):
    """Real Square hours beat transcribed ones for the same weekday."""
    tuesday = _next_weekday(1, date.today())
    EmployeeAvailability.objects.create(
        employee=jackie,
        date=tuesday,
        availability_type=AvailabilityType.AVAILABLE_WINDOW,
        start_time="18:00",
        end_time="22:30",
        source=FALLBACK_AVAILABILITY_SOURCE,
    )
    EmployeeAvailability.objects.create(
        employee=jackie,
        date=tuesday,
        availability_type=AvailabilityType.AVAILABLE_WINDOW,
        start_time="14:00",
        end_time="23:00",
        source=SQUARE_AVAILABILITY_SOURCE,
    )

    client.force_login(manager)
    html = client.get(reverse("employees")).content.decode()

    assert _cell(html, "Jackie Pynn", "Tue") == "14:00-23:00"


def test_hand_entered_hours_outrank_every_sync(client, manager, jackie):
    """Someone typed these in. They are the only availability on file for staff Square
    knows nothing about, and a sync must never quietly overrule them."""
    wednesday = _next_weekday(2, date.today())
    EmployeeAvailability.objects.create(
        employee=jackie,
        date=wednesday,
        availability_type=AvailabilityType.UNKNOWN,
        source=SQUARE_AVAILABILITY_SOURCE,
    )
    EmployeeAvailability.objects.create(
        employee=jackie,
        date=wednesday,
        availability_type=AvailabilityType.AVAILABLE_WINDOW,
        start_time="17:00",
        end_time="21:00",
        source="MANUAL_ENTRY",
    )

    client.force_login(manager)
    html = client.get(reverse("employees")).content.decode()

    assert _cell(html, "Jackie Pynn", "Wed") == "17:00-21:00"


def test_page_offers_a_sync_button(client, manager, jackie):
    client.force_login(manager)
    html = client.get(reverse("employees")).content.decode()
    assert "Sync availability from Square" in html


def test_sync_button_reports_a_failure_instead_of_erroring(client, manager, jackie, monkeypatch):
    """A failed read has to say so on the page. Raising here would show a 500 and
    leave the reason in a log file nobody opens."""
    from scheduling.services import square_pull

    def explode(start, end):
        raise square_pull.SquarePullError("the stored session has expired.")

    monkeypatch.setattr(square_pull, "run_availability_sync", explode)

    client.force_login(manager)
    response = client.post(reverse("employees"), follow=True)
    assert response.status_code == 200
    assert "the stored session has expired." in response.content.decode()


def test_sync_button_says_when_the_answer_is_not_squares(client, manager, jackie, monkeypatch):
    """The fixture fallback returning numbers is not the same as Square returning
    them, and the difference has to be visible - its invisibility is what let
    transcribed hours pass as live ones for weeks."""
    from scheduling.services import square_pull

    monkeypatch.setattr(
        square_pull,
        "run_availability_sync",
        lambda start, end: {"live": False, "known": 10, "total": 20, "completeness": 50.0},
    )

    client.force_login(manager)
    response = client.post(reverse("employees"), follow=True)
    assert "built-in fallback" in response.content.decode()
