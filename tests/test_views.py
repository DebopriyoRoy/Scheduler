import pytest
from django.contrib.auth import get_user_model
from django.urls import reverse


@pytest.mark.django_db
@pytest.mark.parametrize(
    "route_name",
    ("dashboard", "employees", "roles", "square_integration"),
)
def test_management_pages_require_login(client, route_name):
    response = client.get(reverse(route_name))
    assert response.status_code == 302
    assert reverse("login") in response.url


@pytest.mark.django_db
def test_authenticated_user_can_open_dashboard(client):
    user = get_user_model().objects.create_user(username="manager", password="safe-test-password")
    client.force_login(user)
    response = client.get(reverse("dashboard"))
    assert response.status_code == 200
    assert b"Spirit Scheduling Engine" in response.content


@pytest.mark.django_db
def test_square_page_is_safe_without_token(client, monkeypatch):
    monkeypatch.setenv("SQUARE_ENVIRONMENT", "sandbox")
    monkeypatch.setenv("SQUARE_SANDBOX_ACCESS_TOKEN", "")
    monkeypatch.setenv("SQUARE_PRODUCTION_ACCESS_TOKEN", "")


    user = get_user_model().objects.create_user(username="manager", password="safe-test-password")
    client.force_login(user)
    response = client.get(reverse("square_integration"))
    assert response.status_code == 200
    assert b"Not Connected" in response.content
    assert b"No locations loaded" in response.content


@pytest.fixture
def manager(db):
    return get_user_model().objects.create_user(username="mgr", password="safe-test-password")


def _week(html, name):
    """The seven weekday cells rendered for one person on the staff page.

    Matched inside the table row itself: the name also appears in the warning banner
    above the table, and starting there picks up somebody else's cells.
    """
    import re

    rows = re.findall(r"<tr[^>]*>(.*?)</tr>", html, re.S)
    for row in rows:
        if re.search(r'<td class="fw-medium">\s*' + re.escape(name) + r"\b", row):
            return [
                re.sub(r"<[^>]+>", "", c).strip()
                for c in re.findall(
                    r'<td class="text-center small text-nowrap[^"]*">(.*?)</td>', row, re.S
                )
            ]
    raise AssertionError(f"{name} has no row on the staff page")


@pytest.mark.django_db
def test_staff_page_shows_the_latest_availability_not_the_most_common(client, manager):
    """A corrected window must show immediately, not once it outnumbers the old one.

    Availability is kept per date and only the dates a sync covers get refreshed, so
    history keeps superseded values. Kate's Thursday was recorded as 05:30 before that
    transcription was corrected to 17:30; taking the majority put the wrong hours back
    on the page.
    """
    from datetime import date, time

    from scheduling.models import AvailabilityType, Employee, EmployeeAvailability

    kate = Employee.objects.create(first_name="Kate", display_name="Kate")
    for day, start in (
        (date(2026, 9, 3), time(5, 30)),
        (date(2026, 9, 10), time(5, 30)),
        (date(2026, 9, 17), time(17, 30)),  # newest, and the correct one
    ):
        EmployeeAvailability.objects.create(
            employee=kate,
            date=day,
            availability_type=AvailabilityType.AVAILABLE_WINDOW,
            start_time=start,
            end_time=time(21, 30),
            source="LIVE_SQUARE_PRODUCTION",
        )

    client.force_login(manager)
    cells = _week(client.get(reverse("employees")).content.decode(), "Kate")
    assert cells[3] == "17:30-21:30", "the superseded 05:30 window was shown"


@pytest.mark.django_db
def test_a_newer_unknown_clears_hours_from_a_retired_source(client, manager):
    """Stale "available all day" rows must not outlive the feed that wrote them.

    Six staff carry rows from a superseded availability feed claiming they are free all
    day, while every current sync reports nothing for them and the engine refuses to
    schedule them. Showing those hours would contradict the rest of the application.
    """
    from datetime import date

    from scheduling.models import AvailabilityType, Employee, EmployeeAvailability

    person = Employee.objects.create(first_name="Butros", display_name="Butros")
    EmployeeAvailability.objects.create(
        employee=person,
        date=date(2026, 9, 7),
        availability_type=AvailabilityType.AVAILABLE_ALL_DAY,
        source="LIVE_SQUARE_AVAILABILITY",
    )
    EmployeeAvailability.objects.create(
        employee=person,
        date=date(2026, 9, 14),  # same weekday, newer, current feed
        availability_type=AvailabilityType.UNKNOWN,
        source="LIVE_SQUARE_PRODUCTION",
    )

    client.force_login(manager)
    body = client.get(reverse("employees")).content.decode()
    assert _week(body, "Butros")[0] in ("", "—", "&mdash;")
    assert "no availability on file" in body.lower()


@pytest.mark.django_db
def test_hand_entered_availability_outranks_a_square_unknown(client, manager):
    """Typing someone's hours in is the only option for staff Square knows nothing
    about, so a later sync reporting UNKNOWN must not wipe it off the page."""
    from datetime import date, time

    from scheduling.models import AvailabilityType, Employee, EmployeeAvailability

    person = Employee.objects.create(first_name="Svitlana", display_name="Svitlana")
    EmployeeAvailability.objects.create(
        employee=person,
        date=date(2026, 9, 21),
        availability_type=AvailabilityType.AVAILABLE_WINDOW,
        start_time=time(17, 0),
        end_time=time(23, 0),
        source="MANAGEMENT_UI",
    )
    EmployeeAvailability.objects.create(
        employee=person,
        date=date(2026, 9, 28),  # newer sync, but a sync
        availability_type=AvailabilityType.UNKNOWN,
        source="LIVE_SQUARE_PRODUCTION",
    )

    client.force_login(manager)
    assert _week(client.get(reverse("employees")).content.decode(), "Svitlana")[0] == "17:00-23:00"
