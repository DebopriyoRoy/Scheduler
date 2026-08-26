"""Reading Square's version of a roster back into the application.

Synchronisation only ever ran outward, so a bartender swapped in Square, a start time
shifted, or somebody added by hand stayed invisible here - and the next generated
schedule would quietly undo it.
"""

import datetime as dt

import pytest
from django.core.management import call_command

from scheduling.models import (
    AssignmentType,
    Employee,
    Role,
    ScheduleAssignment,
    ScheduleRun,
    ShiftTemplate,
    Show,
    SquareEmployeeMapping,
    SquareLocationMapping,
    SquareRoleMapping,
)
from scheduling.services import square_reconcile as reconcile

NL = dt.timezone(dt.timedelta(hours=-2, minutes=-30))  # America/St_Johns
SHOW_DATE = dt.date(2026, 9, 12)
TM_ID = "TM-JOLEEN"
JOB_SERVICE = "JOB-SERVICE"
JOB_BAR = "JOB-BAR"


class FakeSquare:
    """Stands in for Square. Records any attempt to write, so tests can assert none."""

    def __init__(self, shifts):
        self._shifts = shifts
        self.writes = []

    def search_scheduled_shifts(self, query):
        return self._shifts

    def list_jobs(self):
        return [{"id": JOB_SERVICE, "title": "Service"}, {"id": JOB_BAR, "title": "Bartender"}]

    def create_draft_shift(self, **kwargs):
        self.writes.append(("create", kwargs))
        raise AssertionError("reconciliation must never write to Square")

    def update_draft_shift(self, *args, **kwargs):
        self.writes.append(("update", kwargs))
        raise AssertionError("reconciliation must never write to Square")


def square_shift(start="17:45", end="22:45", job=JOB_SERVICE, team_member=TM_ID):
    return {
        "id": "SQ-1",
        "draft_shift_details": {
            "team_member_id": team_member,
            "job_id": job,
            "start_at": f"{SHOW_DATE}T{start}:00-02:30",
            "end_at": f"{SHOW_DATE}T{end}:00-02:30",
        },
    }


@pytest.fixture
def roster(db):
    call_command("seed_spirit_staff", verbosity=0)
    call_command("seed_scheduling_config", verbosity=0)

    SquareLocationMapping.objects.create(
        environment="production", square_location_id="LOC1", active=True, location_name="Theatre"
    )
    joleen = Employee.objects.get(display_name="Joleen Dickson")
    SquareEmployeeMapping.objects.create(
        employee=joleen, environment="production", square_team_member_id=TM_ID, status="MAPPED"
    )
    for role_name, job_id in (("Server", JOB_SERVICE), ("Bartender", JOB_BAR)):
        SquareRoleMapping.objects.create(
            role=Role.objects.get(name=role_name),
            environment="production",
            square_job_id=job_id,
            status="MAPPED",
        )

    show = Show.objects.create(
        title="Forever Country", date=SHOW_DATE, expected_guests=80, requires_50_50=False
    )
    run = ScheduleRun.objects.create(start_date=SHOW_DATE, end_date=SHOW_DATE)
    ScheduleAssignment.objects.create(
        schedule_run=run,
        show=show,
        employee=joleen,
        role=Role.objects.get(name="Server"),
        assignment_type=AssignmentType.CONFIRMED,
        shift_template=ShiftTemplate.objects.get(code="lead-server"),
        start_datetime=dt.datetime(2026, 9, 12, 17, 45, tzinfo=NL),
        end_datetime=dt.datetime(2026, 9, 12, 22, 45, tzinfo=NL),
        scheduled_paid_hours=5,
        on_call_hours=0,
    )
    return run, joleen, show


def _use(monkeypatch, shifts):
    fake = FakeSquare(shifts)
    monkeypatch.setattr(reconcile, "_production_client", lambda: (fake, "LOC1"))
    return fake


@pytest.mark.django_db
def test_matching_roster_reports_no_differences(roster, monkeypatch):
    run, _, _ = roster
    _use(monkeypatch, [square_shift()])
    report = reconcile.compare_run_with_square(run)
    assert report.matched == 1
    assert not report.has_differences


@pytest.mark.django_db
def test_a_job_changed_in_square_is_detected(roster, monkeypatch):
    """Silencing Square's job-mismatch warning with the dropdown changes what someone
    is recorded and paid as, so it has to surface here."""
    run, _, _ = roster
    _use(monkeypatch, [square_shift(job=JOB_BAR)])
    report = reconcile.compare_run_with_square(run)
    diff = report.of_kind(reconcile.EDITED_IN_SQUARE)[0]
    assert diff.local["job"] == "Service"
    assert diff.square["job"] == "Bartender"


@pytest.mark.django_db
def test_a_shift_removed_in_square_is_detected(roster, monkeypatch):
    run, _, _ = roster
    _use(monkeypatch, [])
    report = reconcile.compare_run_with_square(run)
    assert len(report.of_kind(reconcile.REMOVED_FROM_SQUARE)) == 1


@pytest.mark.django_db
def test_someone_added_in_square_is_flagged_when_unavailable(roster, monkeypatch):
    """Staff added by hand often have no availability recorded, which is exactly why
    the engine never picked them. The shift is real; the gap in the data is the point."""
    run, _, _ = roster
    extra = Employee.objects.get(display_name="Daniel")
    SquareEmployeeMapping.objects.create(
        employee=extra, environment="production", square_team_member_id="TM-DAN", status="MAPPED"
    )
    _use(monkeypatch, [square_shift(), square_shift(job=JOB_BAR, team_member="TM-DAN")])

    report = reconcile.compare_run_with_square(run)
    added = report.of_kind(reconcile.ADDED_IN_SQUARE)
    assert [d.employee_name for d in added] == ["Daniel"]
    assert added[0].availability_note, "no availability on file should be reported"


@pytest.mark.django_db
def test_adopting_updates_the_roster_and_never_writes_to_square(roster, monkeypatch):
    run, joleen, _ = roster
    fake = _use(monkeypatch, [square_shift(start="18:15", end="23:15", job=JOB_BAR)])

    report = reconcile.compare_run_with_square(run)
    applied = reconcile.adopt_square_version(run, report)

    assert applied["updated"] == 1
    assignment = ScheduleAssignment.objects.get(schedule_run=run, employee=joleen)
    assert assignment.role.name == "Bartender"
    assert assignment.manually_overridden, "adopted shifts must survive a regeneration"
    assert "Square" in assignment.override_reason
    assert fake.writes == [], "nothing may be written back to Square"

    assert not reconcile.compare_run_with_square(run).has_differences
