"""The one-show roster screens: steps 1-4 and the hand-over to review."""

from datetime import date, timedelta

import pytest
from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.urls import reverse
from django.utils import timezone

from scheduling.integrations.square_availability.service import SQUARE_AVAILABILITY_SOURCE
from scheduling.models import (
    AvailabilityOverride,
    AvailabilityType,
    Employee,
    EmployeeAvailability,
    EmployeeTimeOff,
    OverrideAction,
    ScheduleRun,
    Show,
    SquareAvailabilitySyncRun,
    TimeOffSource,
    TimeOffStatus,
)
from scheduling.services.freshness import availability_freshness
from scheduling.services.overrides import record_override

DAY = date(2026, 11, 20)


@pytest.fixture
def manager(db):
    return get_user_model().objects.create_user(username="mgr", password="safe-test-password")


@pytest.fixture
def client_in(client, manager):
    client.force_login(manager)
    return client


@pytest.fixture
def staff(db):
    call_command("seed_spirit_staff", verbosity=0)
    call_command("seed_scheduling_config", verbosity=0)
    return list(Employee.objects.filter(active=True))


@pytest.fixture
def show(staff):
    show = Show.objects.create(title="The Blizzard of Oz", date=DAY, expected_guests=158)
    for employee in staff:
        EmployeeAvailability.objects.create(
            employee=employee,
            date=DAY,
            availability_type=AvailabilityType.AVAILABLE_ALL_DAY,
            source=SQUARE_AVAILABILITY_SOURCE,
        )
    return show


def _fresh_square_read(hours_ago=1):
    run = SquareAvailabilitySyncRun.objects.create(
        start_date=DAY, end_date=DAY, status=SquareAvailabilitySyncRun.SyncStatus.SUCCESS
    )
    read_at = timezone.now() - timedelta(hours=hours_ago)
    SquareAvailabilitySyncRun.objects.filter(pk=run.pk).update(
        started_at=read_at, completed_at=read_at
    )
    return run


@pytest.mark.django_db
@pytest.mark.parametrize("name", ["roster_new"])
def test_roster_pages_require_login(client, name):
    response = client.get(reverse(name))
    assert response.status_code == 302


@pytest.mark.django_db
def test_step_one_lists_the_show_and_its_rules(client_in, show):
    response = client_in.get(reverse("roster_new"), {"from": "2026-11-16", "show": show.pk})
    body = response.content.decode()
    assert response.status_code == 200
    assert "The Blizzard of Oz" in body
    assert "Regular evening show" in body
    assert "Chosen automatically" in body


@pytest.mark.django_db
def test_changing_the_guest_count_keeps_the_old_one_in_the_notes(client_in, show):
    response = client_in.post(reverse("roster_guests", args=[show.pk]), {"expected_guests": 165})
    assert response.status_code == 302
    show.refresh_from_db()
    assert show.expected_guests == 165
    assert "from 158 to 165 by mgr" in show.notes


@pytest.mark.django_db
def test_a_guest_count_over_capacity_is_refused(client_in, show):
    client_in.post(reverse("roster_guests", args=[show.pk]), {"expected_guests": 190})
    show.refresh_from_db()
    assert show.expected_guests == 158


@pytest.mark.django_db
def test_availability_page_shows_square_and_manual_changes(client_in, show):
    olena = Employee.objects.get(display_name="Olena")
    record_override(olena, DAY, OverrideAction.REMOVE, "Training at Mile One")

    response = client_in.get(reverse("roster_availability", args=[show.pk]))
    body = response.content.decode()
    assert response.status_code == 200
    assert "Training at Mile One" in body
    assert "Unavailable (manual)" in body


@pytest.mark.django_db
def test_a_manual_change_can_be_added_from_the_page(client_in, show, manager):
    olena = Employee.objects.get(display_name="Olena")
    response = client_in.post(
        reverse("roster_availability", args=[show.pk]),
        {
            "action": "add",
            "employee": olena.pk,
            "change": "REMOVE",
            "start_time": "",
            "end_time": "",
            "reason": "Swapped with Megan",
        },
    )
    assert response.status_code == 302
    override = AvailabilityOverride.objects.get(employee=olena, date=DAY, active=True)
    assert override.action == OverrideAction.REMOVE
    assert override.created_by == manager


@pytest.mark.django_db
def test_an_added_window_is_saved_with_its_hours(client_in, show):
    olena = Employee.objects.get(display_name="Olena")
    client_in.post(
        reverse("roster_availability", args=[show.pk]),
        {
            "action": "add",
            "employee": olena.pk,
            "change": "ADD",
            "start_time": "18:45",
            "end_time": "23:00",
            "reason": "Called in, can bus",
        },
    )
    override = AvailabilityOverride.objects.get(employee=olena, active=True)
    assert f"{override.start_time:%H:%M}-{override.end_time:%H:%M}" == "18:45-23:00"


@pytest.mark.django_db
def test_a_change_without_a_reason_is_refused_on_the_page(client_in, show):
    olena = Employee.objects.get(display_name="Olena")
    response = client_in.post(
        reverse("roster_availability", args=[show.pk]),
        {"action": "add", "employee": olena.pk, "change": "REMOVE", "reason": ""},
    )
    assert response.status_code == 200
    assert not AvailabilityOverride.objects.exists()


@pytest.mark.django_db
def test_a_conflict_blocks_building_until_it_is_settled(client_in, show):
    _fresh_square_read()
    olena = Employee.objects.get(display_name="Olena")
    override = record_override(olena, DAY, OverrideAction.ADD, "Swapped with Megan")
    EmployeeTimeOff.objects.create(
        employee=olena, start_date=DAY, end_date=DAY,
        status=TimeOffStatus.APPROVED, source=TimeOffSource.SQUARE,
    )
    url = reverse("roster_availability", args=[show.pk])

    page = client_in.get(url).content.decode()
    assert "disagrees with newer Square data" in page

    client_in.post(url, {"action": "generate", "allow_unknown": "on"})
    assert not ScheduleRun.objects.exists()

    client_in.post(url, {"action": "keep", "override_id": override.pk})
    override.refresh_from_db()
    assert override.conflict_kept_at is not None

    response = client_in.post(url, {"action": "generate", "allow_unknown": "on"})
    run = ScheduleRun.objects.get()
    assert response.url == reverse("schedule_detail", args=[run.pk])
    assert "Olena" in set(run.assignments.values_list("employee__display_name", flat=True))


@pytest.mark.django_db
def test_use_square_retires_the_change(client_in, show):
    olena = Employee.objects.get(display_name="Olena")
    override = record_override(olena, DAY, OverrideAction.ADD, "Swapped with Megan")
    url = reverse("roster_availability", args=[show.pk])
    client_in.post(url, {"action": "use_square", "override_id": override.pk})
    override.refresh_from_db()
    assert not override.active


@pytest.mark.django_db
def test_stale_square_data_blocks_building_unless_chosen(client_in, show):
    _fresh_square_read(hours_ago=30)
    assert availability_freshness().stale
    url = reverse("roster_availability", args=[show.pk])

    client_in.post(url, {"action": "generate", "allow_unknown": "on"})
    assert not ScheduleRun.objects.exists()

    client_in.post(url, {"action": "generate", "allow_unknown": "on", "use_stale": "on"})
    run = ScheduleRun.objects.get()
    assert "older than 24 hours" in run.notes


@pytest.mark.django_db
def test_building_hands_over_to_the_review_page(client_in, show):
    _fresh_square_read()
    url = reverse("roster_availability", args=[show.pk])
    response = client_in.post(url, {"action": "generate", "allow_unknown": "on"})
    run = ScheduleRun.objects.get()
    assert run.start_date == run.end_date == DAY
    assert response.url == reverse("schedule_detail", args=[run.pk])
    assert "one-show roster screen" in run.notes


@pytest.mark.django_db
def test_entry_points_link_to_the_new_flow(client_in):
    assert reverse("roster_new").encode() in client_in.get(reverse("dashboard")).content
    assert reverse("roster_new").encode() in client_in.get(reverse("schedule_list")).content


@pytest.mark.django_db
def test_override_is_recorded_against_the_user(client_in, show, manager):
    olena = Employee.objects.get(display_name="Olena")
    record_override(olena, DAY, OverrideAction.REMOVE, "Training day", manager)
    assert AvailabilityOverride.objects.get().created_by == manager
