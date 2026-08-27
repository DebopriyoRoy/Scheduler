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
    """With a live session the page offers the read, not the sign-in."""
    from scheduling.integrations import square_session

    square_session.record_session("Test Account", square_session.DEFAULT_AVAILABILITY_URL)

    client.force_login(manager)
    html = client.get(reverse("employees")).content.decode()
    assert "Sync availability from Square" in html
    assert "Connect to Square" not in html


def test_sync_button_reports_a_failure_instead_of_erroring(client, manager, jackie, monkeypatch):
    """A failed read has to say so on the page. Raising here would show a 500 and
    leave the reason in a log file nobody opens.

    Uses a failure that is *not* an expired sign-in: expiry has its own message and
    its own remedy, and is covered separately below.
    """
    from scheduling.services import square_pull

    def explode(start, end):
        raise square_pull.SquarePullError("Square took too long to respond.")

    monkeypatch.setattr(square_pull, "run_availability_sync", explode)

    client.force_login(manager)
    response = client.post(reverse("employees"), follow=True)
    assert response.status_code == 200
    assert "Square took too long to respond." in response.content.decode()


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


def test_expired_session_offers_connect_not_sync(client, manager, jackie, monkeypatch):
    """An expired sign-in must not present a button that cannot work.

    The page used to show a green dot and "connected" purely because a marker file
    existed, so a dead session looked healthy right up until a sync failed.
    """
    from scheduling.integrations import square_session

    monkeypatch.setattr(
        square_session,
        "session_status",
        lambda: square_session.SessionStatus(
            False, "Sign-in expired on 26 Aug 2026. Connect to Square again.", expired=True
        ),
    )

    client.force_login(manager)
    html = client.get(reverse("employees")).content.decode()

    assert "Connect to Square" in html
    assert "Sync availability from Square" not in html
    assert "Square signed this application out." in html


def test_connect_button_reports_success(client, manager, jackie, monkeypatch):
    from scheduling.integrations import square_session
    from scheduling.services import square_pull

    monkeypatch.setattr(
        square_session,
        "session_status",
        lambda: square_session.SessionStatus(False, "Not connected.", expired=True),
    )
    monkeypatch.setattr(
        square_pull,
        "run_square_connect",
        lambda: {"connected": True, "detail": "Connected as Spirit of Newfoundland."},
    )

    client.force_login(manager)
    response = client.post(reverse("employees"), {"action": "connect"}, follow=True)
    assert "Connected as Spirit of Newfoundland." in response.content.decode()


def test_connect_failure_is_shown_not_raised(client, manager, jackie, monkeypatch):
    from scheduling.integrations import square_session
    from scheduling.services import square_pull

    monkeypatch.setattr(
        square_session,
        "session_status",
        lambda: square_session.SessionStatus(False, "Not connected.", expired=True),
    )

    def explode():
        raise square_pull.SquarePullError("the sign-in was not finished in time.")

    monkeypatch.setattr(square_pull, "run_square_connect", explode)

    client.force_login(manager)
    response = client.post(reverse("employees"), {"action": "connect"}, follow=True)
    assert response.status_code == 200
    assert "the sign-in was not finished in time." in response.content.decode()


def test_expired_sync_records_the_expiry(client, manager, jackie, monkeypatch, tmp_path):
    """A sync bounced to the login page must leave the session marked expired.

    Otherwise the page keeps claiming a live connection and the only way to discover
    otherwise is to run another sync and watch it fail the same way.
    """
    monkeypatch.setenv("SPIRIT_SQUARE_SESSION_DIR", str(tmp_path))
    from scheduling.integrations import square_session
    from scheduling.services import square_pull

    square_session.record_session("Test Account", square_session.DEFAULT_AVAILABILITY_URL)
    assert square_session.session_status().connected is True

    def expired(start, end):
        raise square_pull.SquarePullError(
            "Square asked for a sign-in, so the stored session has expired."
        )

    monkeypatch.setattr(square_pull, "run_availability_sync", expired)

    client.force_login(manager)
    response = client.post(reverse("employees"), follow=True)

    assert "Square signed this application out" in response.content.decode()
    status = square_session.session_status()
    assert status.connected is False
    assert status.expired is True


def test_both_windows_show_in_one_weekday_cell(client, manager, jackie):
    """Two windows on the same day are two rows in the database and both must show.

    Khrystyna works 11:00-16:00 and again 18:00-23:00. Showing only the first made
    her look like daytime-only staff who could never work an evening show.
    """
    thursday = _next_weekday(3, date.today())
    for start, end in (("11:00", "16:00"), ("18:00", "23:00")):
        EmployeeAvailability.objects.create(
            employee=jackie,
            date=thursday,
            availability_type=AvailabilityType.AVAILABLE_WINDOW,
            start_time=start,
            end_time=end,
            source=SQUARE_AVAILABILITY_SOURCE,
        )

    client.force_login(manager)
    html = client.get(reverse("employees")).content.decode()

    cell = _cell(html, "Jackie Pynn", "Thu")
    assert "11:00-16:00" in cell
    assert "18:00-23:00" in cell


def test_sync_reaches_back_as_well_as_forward(client, manager, jackie, monkeypatch):
    """A sync only rewrites the dates it is given.

    Rows written before today were never revisited, so a window recorded as
    14:30-00:00 by an earlier, broken parser sat uncorrected because its date fell one
    day before the range began.
    """
    from datetime import date as date_cls

    from scheduling.integrations import square_session
    from scheduling.services import square_pull
    from scheduling.views import AVAILABILITY_SYNC_BACKFILL_DAYS, AVAILABILITY_SYNC_DAYS

    square_session.record_session("Test", square_session.DEFAULT_AVAILABILITY_URL)
    seen = {}

    def capture(start, end):
        seen["start"], seen["end"] = start, end
        return {"live": True, "known": 1, "total": 1, "completeness": 100.0}

    monkeypatch.setattr(square_pull, "run_availability_sync", capture)

    client.force_login(manager)
    client.post(reverse("employees"))

    assert seen["start"] < date_cls.today(), "the sync never revisits older rows"
    assert (date_cls.today() - seen["start"]).days == AVAILABILITY_SYNC_BACKFILL_DAYS
    assert (seen["end"] - date_cls.today()).days == AVAILABILITY_SYNC_DAYS
