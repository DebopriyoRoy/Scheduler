import csv
from datetime import date, datetime

import requests
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import Count, Q
from django.http import HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse

from integrations.square import SquareClient, SquareConfig, SquareEnvironment
from integrations.square.exceptions import SquareIntegrationError
from scheduling.exports.csv_export import detailed_schedule_csv
from scheduling.exports.excel import schedule_workbook_bytes
from scheduling.exports.pdf_export import schedule_pdf_bytes
from scheduling.forms import (
    AvailabilityUploadForm,
    CalendarImportForm,
    FiftyFiftyRotationForm,
    OfficeRotationForm,
    OverrideAssignmentForm,
    ScheduleGenerateForm,
    ShowForm,
)
from scheduling.importers.availability import (
    AvailabilityCSVError,
    import_availability_rows,
    parse_availability_csv,
)
from scheduling.importers.calendar import SpiritCalendarImporter
from scheduling.models import (
    DEFAULT_EXPECTED_GUESTS,
    AvailabilityType,
    Employee,
    EmployeeAvailability,
    FiftyFiftyRotationConfig,
    MappingStatus,
    OfficeRotationConfig,
    Role,
    ScheduleAssignment,
    ScheduleRun,
    ScheduleRunStatus,
    SchedulingWarning,
    Show,
    SquareEmployeeMapping,
    SquareEnvironmentChoices,
    SquareRoleMapping,
    SquareSyncAuditAction,
    SquareSyncAuditLog,
    WarningSeverity,
)
from scheduling.services.engine import IncompleteAvailabilityError, SchedulingEngine
from scheduling.services.metrics import metrics_for_employee
from scheduling.services.square_production_sync import (
    EXPECTED_STAFF_NAMES,
    SquareProductionSyncError,
    approve_manual_employee_mapping,
    create_production_pilot_shift,
    mark_pilot_verified,
    preview_production_sync,
    sync_full_production_schedule,
    sync_production_jobs,
    sync_production_team_members,
)
from scheduling.services.square_sync import (
    SquareSyncError,
    sync_schedule_to_sandbox,
    validate_schedule_for_sync,
)
from scheduling.services.workflow import approve_schedule, override_assignment, resolve_warning


def square_connection_context() -> dict[str, object]:
    context: dict[str, object] = {
        "connection_status": "Not Connected",
        "environment": "Not configured",
        "locations": [],
        "error_message": "",
    }
    try:
        config = SquareConfig.from_env()
        context["environment"] = config.environment.value.title()
        if not config.token_is_configured:
            context["connection_status"] = "Not Connected"
        elif config.environment is SquareEnvironment.SANDBOX:
            context["locations"] = SquareClient(config).test_connection()
            context["connection_status"] = "Connected to Sandbox"
        elif config.environment is SquareEnvironment.PRODUCTION:
            context["locations"] = SquareClient(config).test_connection()
            context["connection_status"] = "Connected to Production"
    except SquareIntegrationError as exc:
        context["connection_status"] = "Connection Error"
        context["error_message"] = str(exc)
    return context


@login_required
def dashboard(request):
    square_context = square_connection_context()
    return render(
        request,
        "scheduling/dashboard.html",
        {
            "employee_count": Employee.objects.filter(active=True).count(),
            "role_count": Role.objects.count(),
            "show_count": Show.objects.filter(active=True).count(),
            "schedule_count": ScheduleRun.objects.count(),
            "square_connection_status": square_context["connection_status"],
        },
    )


@login_required
def employees(request):
    employee_list = Employee.objects.prefetch_related("employee_roles__role")
    return render(request, "scheduling/employees.html", {"employees": employee_list})


@login_required
def roles(request):
    role_list = Role.objects.annotate(
        active_employee_count=Count(
            "employee_roles",
            filter=Q(employee_roles__active=True, employee_roles__employee__active=True),
        )
    )
    return render(request, "scheduling/roles.html", {"roles": role_list})


@login_required
def square_integration(request):
    return render(request, "scheduling/square_integration.html", square_connection_context())


@login_required
def show_list(request):
    """List shows, scoped to a date range and hiding retired rows by default.

    Deactivated shows are kept forever because approved schedules reference them, but
    listing every one of them alongside the live show for the same night made the page
    look like it held three copies of each event.
    """
    start = _parse_optional_date(request.GET.get("start"))
    end = _parse_optional_date(request.GET.get("end"))
    include_inactive = request.GET.get("inactive") == "1"

    shows = Show.objects.all()
    if not include_inactive:
        shows = shows.filter(active=True)
    if start and end:
        if end < start:
            start, end = end, start
        shows = shows.filter(date__range=(start, end))

    initial = {}
    if start:
        initial["start_date"] = start
    if end:
        initial["end_date"] = end

    return render(
        request,
        "scheduling/show_list.html",
        {
            "shows": shows.order_by("date", "start_time"),
            "import_form": CalendarImportForm(initial=initial or None),
            "filter_start": start,
            "filter_end": end,
            "include_inactive": include_inactive,
            "hidden_count": (
                Show.objects.filter(active=False).count() if not include_inactive else 0
            ),
            "default_guests": DEFAULT_EXPECTED_GUESTS,
        },
    )


@login_required
def show_edit(request, show_id=None):
    show = get_object_or_404(Show, pk=show_id) if show_id else None
    form = ShowForm(request.POST or None, instance=show)
    if request.method == "POST" and form.is_valid():
        saved = form.save()
        messages.success(request, f"Saved {saved.title} on {saved.date:%B %d, %Y}.")
        return redirect("show_list")
    return render(request, "scheduling/show_form.html", {"form": form, "show": show})


@login_required
def show_deactivate(request, show_id):
    show = get_object_or_404(Show, pk=show_id)
    if request.method == "POST":
        show.active = False
        show.save(update_fields=["active"])
        messages.success(request, f"Deactivated {show.title}; it was not deleted.")
    return redirect("show_list")


@login_required
def show_import(request):
    if request.method != "POST":
        return redirect("show_list")
    form = CalendarImportForm(request.POST)
    if not form.is_valid():
        messages.error(request, "Correct the import date range and try again.")
        return redirect("show_list")

    start = form.cleaned_data["start_date"]
    end = form.cleaned_data["end_date"]
    try:
        summary = SpiritCalendarImporter().import_range(start, end)
        messages.success(
            request,
            f"Imported {start:%d %b %Y} to {end:%d %b %Y} from the live calendar: "
            f"{summary.created} added, {summary.updated} updated. "
            "Nothing was duplicated or deleted.",
        )
    except (requests.RequestException, ValueError) as exc:
        messages.error(request, f"The live calendar could not be read: {exc}")
        return redirect("show_list")

    # Return to the list showing exactly the dates that were just imported.
    return redirect(f"{reverse('show_list')}?start={start:%Y-%m-%d}&end={end:%Y-%m-%d}")


def _parse_optional_date(value: str | None) -> date | None:
    """Parse a date filter, returning None when absent or unparseable."""
    if not value:
        return None
    try:
        return datetime.strptime(value, "%Y-%m-%d").date()
    except ValueError:
        return None


def _parse_selected_date(value: str | None) -> date:
    if value:
        try:
            return datetime.strptime(value, "%Y-%m-%d").date()
        except ValueError:
            pass
    next_show = Show.objects.filter(active=True).order_by("date").first()
    return next_show.date if next_show else date(2026, 9, 7)


def _copy_dates(raw: str, selected_date: date) -> list[date]:
    dates = [selected_date]
    for value in raw.replace("\n", ",").split(","):
        value = value.strip()
        if value:
            dates.append(datetime.strptime(value, "%Y-%m-%d").date())
    return sorted(set(dates))


@login_required
@transaction.atomic
def availability(request):
    selected_date = _parse_selected_date(request.GET.get("date") or request.POST.get("date"))
    upload_form = AvailabilityUploadForm()
    preview_rows = request.session.get("availability_import_preview")
    if request.method == "POST":
        action = request.POST.get("action")
        if action == "save_bulk":
            try:
                dates = _copy_dates(request.POST.get("copy_dates", ""), selected_date)
                saved = 0
                for employee in Employee.objects.filter(active=True):
                    availability_type = request.POST.get(
                        f"availability_{employee.pk}", AvailabilityType.UNKNOWN
                    )
                    start_time = request.POST.get(f"start_{employee.pk}") or None
                    end_time = request.POST.get(f"end_{employee.pk}") or None
                    notes = request.POST.get(f"notes_{employee.pk}", "").strip()
                    for target_date in dates:
                        entry = EmployeeAvailability(
                            employee=employee,
                            date=target_date,
                            availability_type=availability_type,
                            start_time=start_time,
                            end_time=end_time,
                            source="MANAGEMENT_UI",
                            notes=notes,
                        )
                        entry.full_clean(exclude=("available",))
                        EmployeeAvailability.objects.update_or_create(
                            employee=employee,
                            date=target_date,
                            defaults={
                                "availability_type": availability_type,
                                "start_time": start_time,
                                "end_time": end_time,
                                "source": "MANAGEMENT_UI",
                                "notes": notes,
                            },
                        )
                        saved += 1
                messages.success(request, f"Saved {saved} availability entries.")
                return redirect(f"/availability/?date={selected_date.isoformat()}")
            except (ValueError, ValidationError) as exc:
                messages.error(request, str(exc))
        elif action == "upload_csv":
            upload_form = AvailabilityUploadForm(request.POST, request.FILES)
            if upload_form.is_valid():
                try:
                    rows = parse_availability_csv(upload_form.cleaned_data["csv_file"].read())
                    request.session["availability_import_preview"] = [
                        row.session_dict() for row in rows
                    ]
                    preview_rows = request.session["availability_import_preview"]
                    messages.info(request, f"Validated {len(rows)} rows. Confirm to import them.")
                except AvailabilityCSVError as exc:
                    for error in exc.errors:
                        messages.error(request, error)
        elif action == "confirm_csv" and preview_rows:
            count = import_availability_rows(preview_rows)
            request.session.pop("availability_import_preview", None)
            preview_rows = None
            messages.success(request, f"Imported {count} availability entries atomically.")
            return redirect(f"/availability/?date={selected_date.isoformat()}")

    entries = {
        entry.employee_id: entry
        for entry in EmployeeAvailability.objects.filter(date=selected_date)
    }
    rows = [
        {"employee": employee, "entry": entries.get(employee.pk)}
        for employee in Employee.objects.filter(active=True)
    ]
    return render(
        request,
        "scheduling/availability.html",
        {
            "selected_date": selected_date,
            "rows": rows,
            "availability_types": AvailabilityType.choices,
            "upload_form": upload_form,
            "preview_rows": preview_rows,
        },
    )


@login_required
def availability_template(request):
    start_date = _parse_selected_date(request.GET.get("start"))
    end_date = _parse_selected_date(request.GET.get("end"))
    if end_date < start_date:
        end_date = start_date
    show_dates = list(
        Show.objects.filter(active=True, date__range=(start_date, end_date))
        .values_list("date", flat=True)
        .distinct()
    )
    response = HttpResponse(content_type="text/csv")
    response["Content-Disposition"] = (
        f'attachment; filename="availability-{start_date}-{end_date}.csv"'
    )
    writer = csv.writer(response)
    writer.writerow(["employee", "date", "available", "start_time", "end_time", "notes"])
    for employee in Employee.objects.filter(active=True):
        for show_date in show_dates:
            writer.writerow([employee.display_name, show_date.isoformat(), "", "", "", ""])
    return response


@login_required
def rotation_configuration(request):
    office = OfficeRotationConfig.objects.first()
    fifty = FiftyFiftyRotationConfig.objects.first()
    office_form = OfficeRotationForm(prefix="office", instance=office)
    fifty_form = FiftyFiftyRotationForm(prefix="fifty", instance=fifty)
    if request.method == "POST":
        if request.POST.get("action") == "office":
            office_form = OfficeRotationForm(request.POST, prefix="office", instance=office)
            if office_form.is_valid():
                office_form.save()
                messages.success(request, "Weekend office rotation seed saved.")
                return redirect("rotation_configuration")
        else:
            fifty_form = FiftyFiftyRotationForm(request.POST, prefix="fifty", instance=fifty)
            if fifty_form.is_valid():
                fifty_form.save()
                messages.success(request, "50/50 rotation seed saved.")
                return redirect("rotation_configuration")
    return render(
        request,
        "scheduling/rotation_configuration.html",
        {"office_form": office_form, "fifty_form": fifty_form},
    )


def _generation_summary(start_date: date, end_date: date) -> dict:
    shows = Show.objects.filter(active=True, date__range=(start_date, end_date))
    show_dates = list(shows.values_list("date", flat=True).distinct())
    employee_count = Employee.objects.filter(active=True).count()
    total_cells = employee_count * len(show_dates)
    known = EmployeeAvailability.objects.filter(
        employee__active=True,
        date__in=show_dates,
    ).exclude(availability_type=AvailabilityType.UNKNOWN)
    available = known.filter(
        availability_type__in=[
            AvailabilityType.AVAILABLE_ALL_DAY,
            AvailabilityType.AVAILABLE_WINDOW,
        ]
    )
    return {
        "show_count": shows.count(),
        "guest_count_count": shows.filter(expected_guests__isnull=False).count(),
        "default_guest_count": shows.filter(expected_guests__isnull=True).count(),
        "availability_percent": round(100 * known.count() / total_cells) if total_cells else 100,
        "available_employee_count": available.values("employee_id").distinct().count(),
        "missing_availability_count": max(total_cells - known.count(), 0),
    }


@login_required
def schedule_list(request):
    return render(
        request,
        "scheduling/schedule_list.html",
        {"schedule_runs": ScheduleRun.objects.select_related("created_by", "approved_by")},
    )


@login_required
def schedule_generate(request):
    draft = None
    draft_id = request.GET.get("draft") or request.POST.get("schedule_run_id")
    if draft_id:
        draft = get_object_or_404(ScheduleRun, pk=draft_id, status=ScheduleRunStatus.DRAFT)
    initial = {
        "start_date": draft.start_date if draft else date(2026, 9, 7),
        "end_date": draft.end_date if draft else date(2026, 10, 3),
        "schedule_run_id": draft.pk if draft else None,
    }
    form = ScheduleGenerateForm(request.POST or None, initial=initial)
    if request.method == "POST" and form.is_valid():
        try:
            run = SchedulingEngine().generate(
                form.cleaned_data["start_date"],
                form.cleaned_data["end_date"],
                created_by=request.user,
                allow_shortages=form.cleaned_data["generate_with_shortages"],
                schedule_run=draft,
            )
            messages.success(request, f"Schedule run #{run.pk} generated deterministically.")
            return redirect("schedule_detail", run_id=run.pk)
        except (ValidationError, IncompleteAvailabilityError) as exc:
            form.add_error(None, exc)
    start_date = form.data.get("start_date") if form.is_bound else initial["start_date"]
    end_date = form.data.get("end_date") if form.is_bound else initial["end_date"]
    try:
        if isinstance(start_date, str):
            start_date = datetime.strptime(start_date, "%Y-%m-%d").date()
        if isinstance(end_date, str):
            end_date = datetime.strptime(end_date, "%Y-%m-%d").date()
        summary = _generation_summary(start_date, end_date)
    except (TypeError, ValueError):
        summary = None
    return render(
        request,
        "scheduling/schedule_generate.html",
        {"form": form, "summary": summary, "draft": draft},
    )


def _schedule_rows(schedule_run: ScheduleRun) -> list[dict]:
    codes = (
        "lead-server",
        "server-2",
        "server-3",
        "on-call-server",
        "bartender",
        "on-call-bartender",
        "busser",
        "fifty-fifty",
    )
    shows = Show.objects.filter(
        active=True,
        date__range=(schedule_run.start_date, schedule_run.end_date),
    )
    rows = []
    for show in shows:
        assignments = {
            item.shift_template.code: item
            for item in schedule_run.assignments.filter(show=show).select_related(
                "employee", "shift_template"
            )
        }
        row = {"show": show, "warnings": list(schedule_run.warnings.filter(show=show))}
        row.update({code.replace("-", "_"): assignments.get(code) for code in codes})
        rows.append(row)
    return rows


@login_required
def schedule_detail(request, run_id):
    schedule_run = get_object_or_404(
        ScheduleRun.objects.select_related("created_by", "approved_by"), pk=run_id
    )
    metrics = [
        metrics_for_employee(employee, schedule_run)
        for employee in Employee.objects.filter(active=True)
    ]
    return render(
        request,
        "scheduling/schedule_detail.html",
        {
            "schedule_run": schedule_run,
            "schedule_rows": _schedule_rows(schedule_run),
            "metrics": metrics,
            "hard_errors": schedule_run.warnings.filter(
                severity=WarningSeverity.ERROR, resolved=False
            ).count(),
        },
    )


@login_required
def schedule_override(request, assignment_id):
    assignment = get_object_or_404(
        ScheduleAssignment.objects.select_related(
            "schedule_run", "show", "employee", "role", "shift_template"
        ),
        pk=assignment_id,
    )
    form = OverrideAssignmentForm(request.POST or None, assignment=assignment)
    if request.method == "POST" and form.is_valid():
        try:
            override_assignment(
                assignment,
                form.cleaned_data["employee"],
                form.cleaned_data["override_reason"],
            )
            messages.success(
                request, f"Overrode {assignment.shift_template.name} with an audit reason."
            )
            return redirect("schedule_detail", run_id=assignment.schedule_run_id)
        except ValidationError as exc:
            form.add_error(None, exc)
    return render(
        request,
        "scheduling/schedule_override.html",
        {"assignment": assignment, "form": form},
    )


@login_required
def schedule_approve(request, run_id):
    schedule_run = get_object_or_404(ScheduleRun, pk=run_id)
    if request.method == "POST":
        try:
            approve_schedule(schedule_run, request.user)
            messages.success(request, "Schedule approved locally. No Square shifts were created.")
        except ValidationError as exc:
            messages.error(request, str(exc))
    return redirect("schedule_detail", run_id=run_id)


@login_required
def schedule_new_draft(request, run_id):
    source = get_object_or_404(ScheduleRun, pk=run_id)
    if request.method == "POST":
        draft = ScheduleRun.objects.create(
            start_date=source.start_date,
            end_date=source.end_date,
            status=ScheduleRunStatus.DRAFT,
            created_by=request.user,
            notes=f"New draft based on schedule run #{source.pk}; no assignments copied.",
        )
        messages.info(request, f"Draft #{draft.pk} created. Generate it when ready.")
        return redirect(f"/schedules/generate/?draft={draft.pk}")
    return redirect("schedule_detail", run_id=run_id)


@login_required
def schedule_warning_resolve(request, warning_id):
    warning = get_object_or_404(SchedulingWarning, pk=warning_id)
    if request.method == "POST":
        try:
            resolve_warning(warning, request.POST.get("resolution_note", ""))
            messages.success(request, "Warning resolution recorded.")
        except ValidationError as exc:
            messages.error(request, str(exc))
    return redirect("schedule_detail", run_id=warning.schedule_run_id)


@login_required
def schedule_export_excel(request, run_id):
    schedule_run = get_object_or_404(ScheduleRun, pk=run_id)
    response = HttpResponse(
        schedule_workbook_bytes(schedule_run),
        content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )
    response["Content-Disposition"] = f'attachment; filename="schedule-run-{run_id}.xlsx"'
    return response


@login_required
def schedule_export_csv(request, run_id):
    schedule_run = get_object_or_404(ScheduleRun, pk=run_id)
    response = HttpResponse(detailed_schedule_csv(schedule_run), content_type="text/csv")
    response["Content-Disposition"] = f'attachment; filename="schedule-run-{run_id}.csv"'
    return response


@login_required
def schedule_export_pdf(request, run_id):
    schedule_run = get_object_or_404(ScheduleRun, pk=run_id)
    response = HttpResponse(schedule_pdf_bytes(schedule_run), content_type="application/pdf")
    response["Content-Disposition"] = f'attachment; filename="schedule-run-{run_id}.pdf"'
    return response


@login_required
def schedule_sync_preview(request, run_id):
    schedule_run = get_object_or_404(ScheduleRun, pk=run_id)
    validation = validate_schedule_for_sync(schedule_run)
    return render(
        request,
        "scheduling/schedule_sync_preview.html",
        {
            "schedule_run": schedule_run,
            "validation": validation,
        },
    )


@login_required
def schedule_sync_confirm(request, run_id):
    schedule_run = get_object_or_404(ScheduleRun, pk=run_id)
    if request.method == "POST":
        try:
            result = sync_schedule_to_sandbox(schedule_run)
            messages.success(
                request,
                f"Successfully synced {result['synced_count']} draft shift(s) to Square Sandbox!",
            )
        except SquareSyncError as exc:
            messages.error(request, str(exc))
    return redirect("schedule_detail", run_id=run_id)


@login_required
def square_team_mapping(request):
    if request.method == "POST":
        action = request.POST.get("action")
        if action == "auto_match":
            try:
                res = sync_production_team_members(user=request.user)
                messages.success(
                    request,
                    f"Production Team Auto-Match complete: {res['mapped_exact']} exact mapped, "
                    f"{res['manual_review_required']} review required.",
                )
            except Exception as exc:
                messages.error(request, f"Unable to fetch Production team members: {exc}")
            return redirect("square_team_mapping")
        elif action == "approve_all_candidates":
            review_mappings = SquareEmployeeMapping.objects.filter(
                environment=SquareEnvironmentChoices.PRODUCTION,
                status=MappingStatus.MANUAL_REVIEW_REQUIRED,
            )
            count = 0
            for m in review_mappings:
                if m.square_team_member_id:
                    approve_manual_employee_mapping(
                        m.employee_id, m.square_team_member_id, user=request.user
                    )
                    count += 1
            messages.success(
                request,
                f"Approved {count} candidate team member mappings for Production!",
            )

            return redirect("square_team_mapping")
        elif action == "approve_one":
            emp_id = request.POST.get("employee_id")
            sq_id = request.POST.get("square_team_member_id")
            if emp_id and sq_id:
                approve_manual_employee_mapping(int(emp_id), sq_id, user=request.user)
                messages.success(request, "Mapping approved successfully!")
            return redirect("square_team_mapping")

    mappings = SquareEmployeeMapping.objects.filter(
        environment=SquareEnvironmentChoices.PRODUCTION
    ).select_related("employee")
    return render(
        request,
        "scheduling/square_team_mapping.html",
        {
            "mappings": mappings,
            "expected_staff": EXPECTED_STAFF_NAMES,
        },
    )



@login_required
def square_job_mapping(request):
    if request.method == "POST" and request.POST.get("action") == "auto_match":
        try:
            res = sync_production_jobs(user=request.user)
            messages.success(
                request,
                f"Production Jobs Auto-Match complete: {res['mapped']} mapped, "
                f"{res['unmapped']} unmapped.",
            )
        except Exception as exc:
            messages.error(request, f"Unable to fetch Production jobs: {exc}")
        return redirect("square_job_mapping")

    mappings = SquareRoleMapping.objects.filter(
        environment=SquareEnvironmentChoices.PRODUCTION
    ).select_related("role")
    return render(
        request,
        "scheduling/square_job_mapping.html",
        {
            "mappings": mappings,
        },
    )


@login_required
def square_production_sync_hub(request, run_id):
    schedule_run = get_object_or_404(ScheduleRun, pk=run_id)
    config = SquareConfig.from_env()
    preview = preview_production_sync(schedule_run, user=request.user)

    pilot_audit = SquareSyncAuditLog.objects.filter(
        schedule_run=schedule_run,
        action_type=SquareSyncAuditAction.PRODUCTION_PILOT_CREATED,
    ).first()

    verified_audit = SquareSyncAuditLog.objects.filter(
        action_type=SquareSyncAuditAction.PRODUCTION_PILOT_VERIFIED,
    ).first()

    audit_logs = SquareSyncAuditLog.objects.filter(
        schedule_run=schedule_run
    ).order_by("-created_at")[:20]

    return render(
        request,
        "scheduling/square_production_sync.html",
        {
            "schedule_run": schedule_run,
            "config": config,
            "preview": preview,
            "pilot_audit": pilot_audit,
            "verified_audit": verified_audit,
            "audit_logs": audit_logs,
        },
    )


@login_required
def square_production_pilot_confirm(request, run_id):
    schedule_run = get_object_or_404(ScheduleRun, pk=run_id)
    if request.method == "POST":
        phrase = request.POST.get("confirmation_phrase", "").strip()
        assignment_id = request.POST.get("assignment_id")
        try:
            res = create_production_pilot_shift(
                schedule_run,
                assignment_id=int(assignment_id),
                confirmation_phrase=phrase,
                user=request.user,
            )
            shift_id = res['square_scheduled_shift_id']
            messages.success(
                request,
                f"PRODUCTION PILOT CREATED: Created draft shift {shift_id} in Square Production! "
                "Inspect the draft shift in Square Dashboard manually before proceeding.",
            )
        except (ValueError, SquareProductionSyncError) as exc:
            messages.error(request, str(exc))
    return redirect("square_production_sync_hub", run_id=run_id)


@login_required
def square_production_pilot_verify(request, run_id):
    get_object_or_404(ScheduleRun, pk=run_id)
    if request.method == "POST":
        square_shift_id = request.POST.get("square_shift_id", "").strip()
        mark_pilot_verified(user=request.user, square_shift_id=square_shift_id)
        messages.success(request, "Production Pilot Verified recorded successfully!")
    return redirect("square_production_sync_hub", run_id=run_id)


@login_required
def square_production_full_sync(request, run_id):
    schedule_run = get_object_or_404(ScheduleRun, pk=run_id)
    if request.method == "POST":
        phrase = request.POST.get("confirmation_phrase", "").strip()
        try:
            res = sync_full_production_schedule(
                schedule_run,
                confirmation_phrase=phrase,
                user=request.user,
            )
            count = res['created_count']
            messages.success(
                request,
                f"Full Production Sync Complete: Created {count} draft shifts in Square!",
            )

        except SquareProductionSyncError as exc:
            messages.error(request, str(exc))
    return redirect("square_production_sync_hub", run_id=run_id)


@login_required
def export_production_sync_csv(request, run_id):
    schedule_run = get_object_or_404(ScheduleRun, pk=run_id)
    preview = preview_production_sync(schedule_run, user=request.user)

    response = HttpResponse(content_type="text/csv")
    response["Content-Disposition"] = (
        f'attachment; filename="production_square_sync_results_run_{run_id}.csv"'
    )

    writer = csv.writer(response)
    writer.writerow([
        "Date",
        "Show",
        "Employee",
        "Role",
        "Assignment Type",
        "Start",
        "End",
        "Square Shift ID",
        "Status",
        "Reason",
    ])

    for r in preview.rows:
        writer.writerow([
            r.show_date,
            r.show_title,
            r.employee_name,
            r.role_name,
            r.assignment_type,
            r.start_at,
            r.end_at,
            r.square_team_member_id if r.result_status == "ALREADY_EXISTS" else "",
            r.result_status,
            r.reason,
        ])

    return response


@login_required
def calendar_sync(request):
    """Management view for inspecting and executing Authoritative Spirit Calendar Ingestion."""
    from datetime import date

    from scheduling.integrations.spirit_calendar.service import SpiritCalendarSyncService
    from scheduling.models import CalendarSyncRun

    start_date_str = request.GET.get("start") or request.POST.get("start") or "2026-09-07"
    end_date_str = request.GET.get("end") or request.POST.get("end") or "2026-10-03"

    try:
        start_d = date.fromisoformat(start_date_str)
        end_d = date.fromisoformat(end_date_str)
    except ValueError:
        start_d = date(2026, 9, 7)
        end_d = date(2026, 10, 3)

    service = SpiritCalendarSyncService()
    preview_rows = None
    sync_summary = None

    if request.method == "POST":
        action = request.POST.get("action")
        if action == "preview":
            occurrences, provider_used, r_cnt, e_cnt = service.fetch_with_fallback(start_d, end_d)
            preview_rows = service.generate_preview(occurrences)
            messages.info(
                request,
                f"Preview generated using {provider_used}: Found {len(occurrences)} occurrence(s).",
            )
        elif action == "confirm":
            sync_summary = service.execute_sync(start_d, end_d)
            sr = sync_summary.sync_run
            messages.success(
                request,
                f"Sync Complete ({sr.provider}): Created {sr.events_created}, "
                f"Updated {sr.events_updated}, Unchanged {sr.events_unchanged}.",
            )

    sync_runs = CalendarSyncRun.objects.all()[:10]
    latest_run = sync_runs.first()

    return render(
        request,
        "scheduling/calendar_sync.html",
        {
            "start_date": start_d.isoformat(),
            "end_date": end_d.isoformat(),
            "sync_runs": sync_runs,
            "latest_run": latest_run,
            "preview_rows": preview_rows,
            "sync_summary": sync_summary,
        },
    )


@login_required
def square_availability_sync(request):
    """Management view for inspecting and validating Square Production employee availability."""
    from datetime import date

    from scheduling.integrations.square_availability.service import SquareAvailabilitySyncService
    from scheduling.models import SquareAvailabilitySyncRun

    start_date_str = request.GET.get("start") or request.POST.get("start") or "2026-09-07"
    end_date_str = request.GET.get("end") or request.POST.get("end") or "2026-10-03"

    try:
        start_d = date.fromisoformat(start_date_str)
        end_d = date.fromisoformat(end_date_str)
    except ValueError:
        start_d = date(2026, 9, 7)
        end_d = date(2026, 10, 3)

    service = SquareAvailabilitySyncService()
    analysis = service.execute_sync(start_d, end_d)

    if request.method == "POST":
        messages.success(
            request,
            f"Square Production Availability Refreshed ({analysis.sync_run.provider}): "
            f"{analysis.known_combinations}/{analysis.total_combinations} Known "
            f"({analysis.completeness_pct}% Completeness).",
        )

    sync_runs = SquareAvailabilitySyncRun.objects.all()[:10]
    latest_run = sync_runs.first()

    return render(
        request,
        "scheduling/square_availability_sync.html",
        {
            "start_date": start_d.isoformat(),
            "end_date": end_d.isoformat(),
            "sync_runs": sync_runs,
            "latest_run": latest_run,
            "analysis": analysis,
        },
    )




@login_required
def availability_comparison(request):
    """Side-by-side view of what Square reports against what management entered.

    Square is the operational source for staff who maintain availability there, but six
    of the roster have nothing entered in it at all, so management records theirs by
    hand. Both live in EmployeeAvailability, distinguished by `source`. This page makes
    the two visible together so a disagreement is a decision rather than a surprise
    discovered when someone turns up ineligible.
    """
    from scheduling.integrations.square_availability.service import (
        SQUARE_AVAILABILITY_SOURCE,
    )

    start_date = _parse_selected_date(request.GET.get("start"))
    end_date = _parse_selected_date(request.GET.get("end"))
    if end_date < start_date:
        end_date = start_date

    show_dates = list(
        Show.objects.filter(active=True, date__range=(start_date, end_date))
        .values_list("date", flat=True)
        .distinct()
        .order_by("date")
    )
    employees = list(
        Employee.objects.filter(active=True, excluded_from_automatic_scheduling=False).order_by(
            "display_name"
        )
    )

    entries = EmployeeAvailability.objects.filter(
        employee__in=employees, date__in=show_dates
    ).select_related("employee")

    def describe(rows):
        """Collapse the rows for one employee/date into a readable window string."""
        rows = [r for r in rows if r.availability_type != AvailabilityType.UNKNOWN]
        if not rows:
            return ""
        if any(r.availability_type == AvailabilityType.AVAILABLE_ALL_DAY for r in rows):
            return "All day"
        if all(r.availability_type == AvailabilityType.UNAVAILABLE for r in rows):
            return "Unavailable"
        windows = sorted(
            f"{r.start_time:%H:%M}-{r.end_time:%H:%M}"
            for r in rows
            if r.availability_type == AvailabilityType.AVAILABLE_WINDOW
            and r.start_time
            and r.end_time
        )
        return ", ".join(windows)

    grouped: dict[tuple[int, date], dict[str, list]] = {}
    for entry in entries:
        bucket = grouped.setdefault(
            (entry.employee_id, entry.date), {"square": [], "manual": []}
        )
        key = "square" if entry.source == SQUARE_AVAILABILITY_SOURCE else "manual"
        bucket[key].append(entry)

    STATUS = {
        "match": ("Agree", "success"),
        "conflict": ("Differs", "warning"),
        "square_only": ("Square only", "secondary"),
        "manual_only": ("Entered locally", "info"),
        "none": ("No availability", "light"),
    }

    rows = []
    tally = {key: 0 for key in STATUS}
    for employee in employees:
        cells = []
        for show_date in show_dates:
            bucket = grouped.get((employee.id, show_date), {"square": [], "manual": []})
            square_text = describe(bucket["square"])
            manual_text = describe(bucket["manual"])
            if square_text and manual_text:
                status = "match" if square_text == manual_text else "conflict"
            elif square_text:
                status = "square_only"
            elif manual_text:
                status = "manual_only"
            else:
                status = "none"
            tally[status] += 1
            label, tone = STATUS[status]
            cells.append(
                {
                    "date": show_date,
                    "square": square_text,
                    "manual": manual_text,
                    "status": status,
                    "label": label,
                    "tone": tone,
                }
            )
        rows.append(
            {
                "employee": employee,
                "cells": cells,
                "missing": sum(1 for c in cells if c["status"] == "none"),
            }
        )

    # Staff the engine cannot consider at all: nothing on file from either source.
    blocked = [r["employee"].display_name for r in rows if r["missing"] == len(show_dates)]

    return render(
        request,
        "scheduling/availability_comparison.html",
        {
            "start_date": start_date.isoformat(),
            "end_date": end_date.isoformat(),
            "show_dates": show_dates,
            "rows": rows,
            "tally": [
                {"key": k, "label": STATUS[k][0], "tone": STATUS[k][1], "count": v}
                for k, v in tally.items()
            ],
            "blocked": blocked,
            "total_cells": len(employees) * len(show_dates),
        },
    )
