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
    assert b"Spirit Scheduling Agent" in response.content


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


def _run(status, days_ahead=30, **extra):
    from datetime import date, timedelta

    from scheduling.models import ScheduleRun

    start = date.today() + timedelta(days=days_ahead)
    return ScheduleRun.objects.create(
        start_date=start, end_date=start + timedelta(days=7), status=status, **extra
    )


@pytest.mark.django_db
def test_delete_removes_an_in_progress_run_and_everything_hanging_off_it(client, manager):
    from scheduling.models import (
        ScheduleRun,
        ScheduleRunStatus,
        SchedulingWarning,
        WarningSeverity,
        WarningType,
    )

    client.force_login(manager)
    run = _run(ScheduleRunStatus.NEEDS_REVIEW)
    SchedulingWarning.objects.create(
        schedule_run=run,
        warning_type=WarningType.SERVER_SHORTAGE,
        severity=WarningSeverity.ERROR,
        message="short by one",
    )
    response = client.post(reverse("schedule_delete", args=[run.pk]), follow=True)

    assert response.status_code == 200
    assert not ScheduleRun.objects.filter(pk=run.pk).exists()
    assert not SchedulingWarning.objects.filter(schedule_run_id=run.pk).exists()


@pytest.mark.django_db
def test_delete_ignores_a_get_so_a_stray_link_cannot_destroy_a_run(client, manager):
    from scheduling.models import ScheduleRun, ScheduleRunStatus

    client.force_login(manager)
    run = _run(ScheduleRunStatus.NEEDS_REVIEW)
    response = client.get(reverse("schedule_delete", args=[run.pk]))

    assert response.status_code == 302
    assert ScheduleRun.objects.filter(pk=run.pk).exists()


@pytest.mark.django_db
def test_delete_refuses_a_run_already_sent_to_square(client, manager):
    from scheduling.models import ScheduleRun, ScheduleRunStatus

    client.force_login(manager)
    run = _run(ScheduleRunStatus.SYNCED_TO_SQUARE)
    client.post(reverse("schedule_delete", args=[run.pk]), follow=True)

    assert ScheduleRun.objects.filter(pk=run.pk).exists()


@pytest.mark.django_db
def test_delete_refuses_a_run_that_created_shifts_in_square(client, manager):
    from scheduling.models import (
        ScheduleRun,
        ScheduleRunStatus,
        SquareSyncAuditAction,
        SquareSyncAuditLog,
    )

    client.force_login(manager)
    run = _run(ScheduleRunStatus.NEEDS_REVIEW)
    SquareSyncAuditLog.objects.create(
        schedule_run=run, action_type=SquareSyncAuditAction.PRODUCTION_DRAFT_CREATED
    )
    client.post(reverse("schedule_delete", args=[run.pk]), follow=True)

    assert ScheduleRun.objects.filter(pk=run.pk).exists()


@pytest.mark.django_db
def test_a_previewed_run_is_still_deletable(client, manager):
    """A preview writes an audit row but creates nothing in Square."""
    from scheduling.models import (
        ScheduleRun,
        ScheduleRunStatus,
        SquareSyncAuditAction,
        SquareSyncAuditLog,
    )

    client.force_login(manager)
    run = _run(ScheduleRunStatus.NEEDS_REVIEW)
    SquareSyncAuditLog.objects.create(
        schedule_run=run, action_type=SquareSyncAuditAction.PRODUCTION_SYNC_PREVIEWED
    )
    client.post(reverse("schedule_delete", args=[run.pk]), follow=True)

    assert not ScheduleRun.objects.filter(pk=run.pk).exists()


@pytest.mark.django_db
def test_delete_button_appears_only_for_in_progress_runs(client, manager):
    from scheduling.models import ScheduleRunStatus

    client.force_login(manager)
    _run(ScheduleRunStatus.NEEDS_REVIEW)
    _run(ScheduleRunStatus.SYNCED_TO_SQUARE)
    body = client.get(reverse("schedule_list")).content.decode()

    assert body.count("btn-outline-danger") == 1


@pytest.mark.django_db
def test_deleting_the_same_run_twice_says_so_instead_of_raising_404(client, manager):
    """The browser replays this POST on a back-navigation or reload.

    Reproduces the live failure: the first delete succeeded, the resubmit raised
    Http404 and dumped a debug page over a delete that had actually worked.
    """
    from scheduling.models import ScheduleRun, ScheduleRunStatus

    client.force_login(manager)
    run = _run(ScheduleRunStatus.NEEDS_REVIEW)
    url = reverse("schedule_delete", args=[run.pk])

    first = client.post(url, follow=True)
    second = client.post(url, follow=True)

    assert first.status_code == 200
    assert second.status_code == 200
    assert not ScheduleRun.objects.filter(pk=run.pk).exists()
    assert b"already been deleted" in second.content


@pytest.mark.django_db
def test_removing_from_square_for_a_run_that_is_gone_does_not_raise_404(client, manager):
    from scheduling.models import ScheduleRun

    client.force_login(manager)
    missing = ScheduleRun.objects.count() + 999
    response = client.post(reverse("schedule_square_remove", args=[missing]), follow=True)

    assert response.status_code == 200
    assert b"no longer exists" in response.content


@pytest.mark.django_db
def test_a_blocked_run_shows_what_is_blocking_and_offers_a_way_through(client, manager):
    """Removing the warnings screen left hard errors with no way to clear them.

    Every hard error here is a shortage nobody eligible can fill, so "fill the slot"
    is not always an available answer - without this the run could never be approved
    and so could never reach Square.
    """
    from scheduling.models import (
        ScheduleRunStatus,
        SchedulingWarning,
        WarningSeverity,
        WarningType,
    )

    client.force_login(manager)
    run = _run(ScheduleRunStatus.NEEDS_REVIEW)
    SchedulingWarning.objects.create(
        schedule_run=run,
        warning_type=WarningType.ON_CALL_SERVER_SHORTAGE,
        severity=WarningSeverity.ERROR,
        message="No eligible employee for On-call Server.",
    )
    body = client.get(reverse("schedule_detail", args=[run.pk])).content.decode()

    assert "blocking approval" in body
    assert "Accept" in body


@pytest.mark.django_db
def test_accepting_a_blocker_unblocks_approval_and_the_square_buttons(client, manager):
    from scheduling.models import (
        ScheduleRun,
        ScheduleRunStatus,
        SchedulingWarning,
        WarningSeverity,
        WarningType,
    )

    client.force_login(manager)
    run = _run(ScheduleRunStatus.NEEDS_REVIEW)
    warning = SchedulingWarning.objects.create(
        schedule_run=run,
        warning_type=WarningType.SERVER_SHORTAGE,
        severity=WarningSeverity.ERROR,
        message="No eligible employee for Server 3.",
    )
    run.assignments.model.objects.none()  # run has no assignments; approval also needs some

    client.post(
        reverse("schedule_warning_resolve", args=[warning.pk]),
        {"resolution_note": "running short that night, accepted"},
        follow=True,
    )
    warning.refresh_from_db()
    assert warning.resolved is True

    body = client.get(reverse("schedule_detail", args=[run.pk])).content.decode()
    assert "blocking approval" not in body
    assert ScheduleRun.objects.get(pk=run.pk).status == ScheduleRunStatus.NEEDS_REVIEW


@pytest.mark.django_db
def test_a_clean_run_shows_no_blocking_panel_at_all(client, manager):
    from scheduling.models import ScheduleRunStatus

    client.force_login(manager)
    run = _run(ScheduleRunStatus.NEEDS_REVIEW)
    body = client.get(reverse("schedule_detail", args=[run.pk])).content.decode()

    assert "blocking approval" not in body


@pytest.mark.django_db
def test_the_square_buttons_appear_once_a_run_is_approved(client, manager):
    from scheduling.models import ScheduleRunStatus

    client.force_login(manager)
    run = _run(ScheduleRunStatus.APPROVED)
    body = client.get(reverse("schedule_detail", args=[run.pk])).content.decode()

    assert "Square Production Sync" in body
    assert "Sync to Sandbox" in body


@pytest.mark.django_db
def test_all_blockers_can_be_accepted_at_once(client, manager):
    """A thin month can produce twenty unfillable shortages; one at a time is not
    a workable gate."""
    from scheduling.models import (
        ScheduleRunStatus,
        SchedulingWarning,
        WarningSeverity,
        WarningType,
    )

    client.force_login(manager)
    run = _run(ScheduleRunStatus.NEEDS_REVIEW)
    for _ in range(5):
        SchedulingWarning.objects.create(
            schedule_run=run,
            warning_type=WarningType.SERVER_SHORTAGE,
            severity=WarningSeverity.ERROR,
            message="No eligible employee.",
        )

    client.post(
        reverse("schedule_warnings_accept_all", args=[run.pk]),
        {"resolution_note": "short-staffed month, accepted by management"},
        follow=True,
    )

    assert not run.warnings.filter(severity=WarningSeverity.ERROR, resolved=False).exists()
    # the reason is on each one, not just recorded once
    for warning in run.warnings.all():
        assert warning.resolution_note == "short-staffed month, accepted by management"


@pytest.mark.django_db
def test_accepting_all_requires_a_reason(client, manager):
    from scheduling.models import (
        ScheduleRunStatus,
        SchedulingWarning,
        WarningSeverity,
        WarningType,
    )

    client.force_login(manager)
    run = _run(ScheduleRunStatus.NEEDS_REVIEW)
    SchedulingWarning.objects.create(
        schedule_run=run,
        warning_type=WarningType.SERVER_SHORTAGE,
        severity=WarningSeverity.ERROR,
        message="No eligible employee.",
    )

    client.post(
        reverse("schedule_warnings_accept_all", args=[run.pk]), {"resolution_note": "x"},
        follow=True,
    )

    assert run.warnings.filter(severity=WarningSeverity.ERROR, resolved=False).exists()
