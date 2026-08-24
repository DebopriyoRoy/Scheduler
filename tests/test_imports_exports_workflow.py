from datetime import date
from io import BytesIO

import pytest
from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.core.management import call_command
from django.test import override_settings
from openpyxl import load_workbook

from scheduling.exports.csv_export import detailed_schedule_csv
from scheduling.exports.excel import schedule_workbook_bytes
from scheduling.exports.pdf_export import schedule_pdf_bytes
from scheduling.importers.availability import (
    AvailabilityCSVError,
    import_availability_rows,
    parse_availability_csv,
)
from scheduling.importers.calendar import SpiritCalendarImporter, events_from_html
from scheduling.models import (
    AvailabilityType,
    Employee,
    EmployeeAvailability,
    ScheduleRunStatus,
    SchedulingWarning,
    Show,
    WarningSeverity,
    WarningType,
)
from scheduling.services.engine import SchedulingEngine
from scheduling.services.workflow import approve_schedule, override_assignment


@pytest.fixture
def staff_and_config(db):
    call_command("seed_spirit_staff", verbosity=0)
    call_command("seed_scheduling_config", verbosity=0)
    return list(Employee.objects.filter(active=True))


@pytest.fixture
def management_user(db):
    return get_user_model().objects.create_user(username="manager", password="test-password")


def generated_run(staff, *, requires_50_50=True):
    show = Show.objects.create(
        title="Forever Country",
        date=date(2026, 9, 12),
        expected_guests=100,
        requires_50_50=requires_50_50,
    )
    for employee in staff:
        EmployeeAvailability.objects.create(
            employee=employee,
            date=show.date,
            availability_type=AvailabilityType.AVAILABLE_ALL_DAY,
        )
    return SchedulingEngine().generate(show.date, show.date), show


@pytest.mark.django_db
def test_availability_csv_validates_then_imports_atomically(staff_and_config):
    csv_text = (
        "employee,date,available,start_time,end_time,notes\n"
        "Olena,2026-09-12,yes,15:00,23:00,Can work\n"
        "Kate,2026-09-12,no,,,Away\n"
    )
    rows = parse_availability_csv(csv_text)
    assert EmployeeAvailability.objects.count() == 0
    assert import_availability_rows(rows) == 2
    olena = EmployeeAvailability.objects.get(employee__display_name="Olena")
    kate = EmployeeAvailability.objects.get(employee__display_name="Kate")
    assert olena.availability_type == AvailabilityType.AVAILABLE_WINDOW
    assert kate.availability_type == AvailabilityType.UNAVAILABLE


@pytest.mark.django_db
def test_malformed_availability_csv_imports_nothing(staff_and_config):
    csv_text = (
        "employee,date,available,start_time,end_time,notes\n"
        "Olena,2026-09-12,yes,,,Fine\n"
        "Not A Person,2026-09-12,yes,,,Bad\n"
    )
    with pytest.raises(AvailabilityCSVError, match="unknown employee"):
        parse_availability_csv(csv_text)
    assert EmployeeAvailability.objects.count() == 0


def test_calendar_parser_reads_json_ld_event():
    html = """
    <html><script type="application/ld+json">
    {"@type":"Event","name":"Forever Country","startDate":"2026-09-12T18:30:00-02:30",
    "endDate":"2026-09-12T22:30:00-02:30","url":"https://example.test/forever",
    "location":{"@type":"Place","name":"Theatre Gower"}}
    </script></html>
    """
    events = events_from_html(html, "https://example.test/calendar")
    assert len(events) == 1
    assert events[0].title == "Forever Country"
    assert events[0].date == date(2026, 9, 12)
    assert str(events[0].start_time) == "18:30:00"


def test_calendar_parser_has_human_visible_date_fallback():
    html = "<html><h1>Shift Happens</h1><p>September 19, 2026 6:30 pm – 10:30 pm</p></html>"
    events = events_from_html(html, "https://example.test/shift-happens")
    assert [(event.title, event.date) for event in events] == [("Shift Happens", date(2026, 9, 19))]


@pytest.mark.django_db
def test_calendar_import_is_idempotent_and_does_not_delete_manual_show():
    manual = Show.objects.create(title="Manual", date=date(2026, 9, 20))
    html = """
    <html><script type="application/ld+json">
    {"@type":"Event","name":"Imported","startDate":"2026-09-12T18:30:00-02:30",
    "endDate":"2026-09-12T22:30:00-02:30","url":"https://example.test/imported"}
    </script></html>
    """

    class Response:
        text = html

        def raise_for_status(self):
            return None

    class Session:
        headers = {}

        def get(self, *args, **kwargs):
            return Response()

    importer = SpiritCalendarImporter(session=Session())
    first = importer.import_range(date(2026, 9, 7), date(2026, 10, 3))
    second = importer.import_range(date(2026, 9, 7), date(2026, 10, 3))
    assert first.created == 1
    assert second.created == 0
    assert Show.objects.filter(pk=manual.pk).exists()


@pytest.mark.django_db
def test_excel_csv_and_pdf_exports_are_complete(staff_and_config):
    run, show = generated_run(staff_and_config)
    workbook_bytes = schedule_workbook_bytes(run)
    workbook = load_workbook(BytesIO(workbook_bytes), data_only=False)
    assert workbook.sheetnames == [
        "Schedule",
        "Detailed Assignments",
        "Employee Totals",
        "Warnings",
        "Assumptions",
    ]
    assert workbook["Schedule"]["A2"].value.date() == show.date
    assert workbook["Schedule"]["C2"].value == show.title
    assert workbook["Detailed Assignments"].max_row == 9
    detailed_rows = list(workbook["Detailed Assignments"].iter_rows(values_only=True))
    lead_row = next(row for row in detailed_rows if row[3] == "Server" and row[5] == "15:00")
    assert lead_row[6] == "21:30"
    assert workbook["Employee Totals"].max_row == 18
    assert "Forever Country" in detailed_schedule_csv(run)
    pdf = schedule_pdf_bytes(run)
    assert pdf.startswith(b"%PDF")
    assert len(pdf) > 3000


@pytest.mark.django_db
def test_valid_override_is_audited_and_invalid_override_is_blocked(staff_and_config):
    run, _ = generated_run(staff_and_config, requires_50_50=False)
    assignment = run.assignments.get(shift_template__code="lead-server")
    replacement = next(
        employee
        for employee in Employee.objects.filter(employee_roles__role__name="Server")
        if not run.assignments.filter(show=assignment.show, employee=employee).exists()
    )
    result = override_assignment(assignment, replacement, "Management requested this change")
    assert result.manually_overridden is True
    assert result.override_reason == "Management requested this change"
    unavailable = Employee.objects.get(display_name="Olena")
    EmployeeAvailability.objects.filter(employee=unavailable, date=assignment.show.date).update(
        availability_type=AvailabilityType.UNAVAILABLE,
        available=False,
    )
    with pytest.raises(ValidationError, match="ineligible"):
        override_assignment(result, unavailable, "Try an invalid unavailable person")


@pytest.mark.django_db
def test_approval_blocks_errors_and_records_success(staff_and_config, management_user):
    run, _ = generated_run(staff_and_config)
    warning = SchedulingWarning.objects.create(
        schedule_run=run,
        warning_type=WarningType.SERVER_SHORTAGE,
        severity=WarningSeverity.ERROR,
        message="Test hard error",
    )
    with pytest.raises(ValidationError, match="Resolve"):
        approve_schedule(run, management_user)
    warning.resolved = True
    warning.resolution_note = "Management verified alternate coverage."
    warning.save()
    approve_schedule(run, management_user)
    run.refresh_from_db()
    assert run.status == ScheduleRunStatus.APPROVED
    assert run.approved_by == management_user
    assert run.approved_at is not None


@pytest.mark.django_db
def test_management_pages_and_export_endpoints(client, management_user, staff_and_config):
    run, _ = generated_run(staff_and_config)
    client.force_login(management_user)
    for url in (
        "/shows/",
        "/availability/",
        "/configuration/rotations/",
        "/schedules/",
        "/schedules/generate/",
        f"/schedules/{run.pk}/",
    ):
        assert client.get(url).status_code == 200
    excel_response = client.get(f"/schedules/{run.pk}/export.xlsx")
    assert excel_response.status_code == 200
    assert "spreadsheetml" in excel_response["Content-Type"]
    assert client.get(f"/schedules/{run.pk}/export.csv").status_code == 200
    assert client.get(f"/schedules/{run.pk}/export.pdf").content.startswith(b"%PDF")


@pytest.mark.django_db
def test_schedule_generation_page_requires_explicit_shortage_choice(
    client,
    management_user,
    staff_and_config,
):
    Show.objects.create(title="Missing Availability", date=date(2026, 9, 12))
    client.force_login(management_user)
    response = client.post(
        "/schedules/generate/",
        {"start_date": "2026-09-07", "end_date": "2026-10-03"},
    )
    assert response.status_code == 200
    assert b"Choose Generate with shortages" in response.content


@pytest.mark.django_db
def test_complete_browser_workflow_generates_overrides_and_approves(
    client,
    management_user,
    staff_and_config,
):
    show = Show.objects.create(
        title="Browser Workflow Show",
        date=date(2026, 9, 12),
        expected_guests=100,
        requires_50_50=True,
    )
    for employee in staff_and_config:
        EmployeeAvailability.objects.create(
            employee=employee,
            date=show.date,
            availability_type=AvailabilityType.AVAILABLE_ALL_DAY,
        )
    client.force_login(management_user)
    response = client.post(
        "/schedules/generate/",
        {"start_date": "2026-09-07", "end_date": "2026-10-03"},
    )
    assert response.status_code == 302
    schedule_run = show.schedule_assignments.first().schedule_run
    assert schedule_run.status == ScheduleRunStatus.GENERATED
    detail = client.get(response["Location"])
    assert detail.status_code == 200
    assert b"Browser Workflow Show" in detail.content
    assignment = schedule_run.assignments.get(shift_template__code="lead-server")
    replacement = next(
        employee
        for employee in Employee.objects.filter(employee_roles__role__name="Server")
        if not schedule_run.assignments.filter(show=show, employee=employee).exists()
    )
    override_response = client.post(
        f"/schedules/assignments/{assignment.pk}/override/",
        {"employee": replacement.pk, "override_reason": "Browser workflow validation"},
    )
    assert override_response.status_code == 302
    assignment.refresh_from_db()
    assert assignment.manually_overridden is True
    approval_response = client.post(f"/schedules/{schedule_run.pk}/approve/")
    assert approval_response.status_code == 302
    schedule_run.refresh_from_db()
    assert schedule_run.status == ScheduleRunStatus.APPROVED


@pytest.mark.django_db
@override_settings(DEBUG=True)
def test_demo_seed_is_idempotent_and_clearly_isolated():
    call_command("seed_schedule_demo", verbosity=0)
    call_command("seed_schedule_demo", verbosity=0)
    assert Show.objects.count() == 6
    assert not Show.objects.exclude(source=Show.Source.DEMO).exists()
    assert EmployeeAvailability.objects.count() == 102
    assert not EmployeeAvailability.objects.exclude(source="DEMO").exists()
