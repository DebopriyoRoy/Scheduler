import csv
import logging
import os
from datetime import date, datetime, timedelta

from django.conf import settings
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import Count, Q
from django.http import HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.template.defaultfilters import pluralize
from django.urls import reverse

from integrations.square import SquareClient, SquareConfig, SquareEnvironment
from integrations.square.exceptions import (
    SquareIntegrationError,
    SquareProductionWritesDisabledError,
)
from scheduling.exports.csv_export import detailed_schedule_csv
from scheduling.exports.excel import schedule_workbook_bytes
from scheduling.exports.pdf_export import schedule_pdf_bytes
from scheduling.forms import (
    AvailabilityUploadForm,
    CalendarImportForm,
    FiftyFiftyRotationForm,
    FillAssignmentForm,
    OfficeRotationForm,
    OverrideAssignmentForm,
    ScheduleGenerateForm,
    ShowForm,
    TimeOffForm,
)
from scheduling.importers.availability import (
    AvailabilityCSVError,
    import_availability_rows,
    parse_availability_csv,
)
from scheduling.models import (
    DEFAULT_EXPECTED_GUESTS,
    AvailabilityType,
    CalendarSyncRun,
    Department,
    Employee,
    EmployeeAvailability,
    EmployeeTimeOff,
    FiftyFiftyRotationConfig,
    MappingStatus,
    OfficeRotationConfig,
    Role,
    ScheduleAssignment,
    ScheduleRun,
    ScheduleRunStatus,
    SchedulingWarning,
    ShiftTemplate,
    Show,
    SquareAvailabilitySyncRun,
    SquareEmployeeMapping,
    SquareEnvironmentChoices,
    SquareRoleMapping,
    SquareSyncAuditAction,
    SquareSyncAuditLog,
    TimeOffSource,
    TimeOffStatus,
    WarningSeverity,
    WarningType,
)
from scheduling.services.calendar_import import CalendarImportError, run_calendar_import
from scheduling.services.engine import (
    IncompleteAvailabilityError,
    SchedulingEngine,
    shift_window_for,
)
from scheduling.services.metrics import metrics_for_employee
from scheduling.services.square_production_sync import (
    EXPECTED_STAFF_NAMES,
    SquareProductionSyncError,
    approve_manual_employee_mapping,
    create_production_pilot_shift,
    has_untracked_square_creations,
    mark_pilot_verified,
    preview_production_sync,
    remove_run_from_square,
    shifts_still_in_square,
    shifts_still_in_square_by_run,
    sync_full_production_schedule,
    sync_production_jobs,
    sync_production_team_members,
    update_run_in_square,
)
from scheduling.services.square_reconcile import (
    SquareReadError,
    adopt_square_version,
    compare_run_with_square,
)
from scheduling.services.square_sync import (
    SquareSyncError,
    sync_schedule_to_sandbox,
    validate_schedule_for_sync,
)
from scheduling.services.workflow import (
    approve_schedule,
    fill_assignment,
    override_assignment,
    resolve_warning,
)

logger = logging.getLogger(__name__)


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
            "square_environment": square_context["environment"],
        },
    )


# How long a roster-page sync reaches forward. Square holds availability as a
# repeating weekly pattern, but this application stores it per date, so a sync has to
# write a row for every date it wants covered. Eighteen weeks comfortably spans the
# two-week publishing lead plus the Christmas season.
AVAILABILITY_SYNC_DAYS = 126

# How far *back* a sync also reaches. A sync only rewrites the dates it is given, so
# rows dated before today were never revisited and kept whatever an older source had
# put there - Yana carried a 14:30-00:00 Tuesday from the hand-typed fixture while
# Square says 14:30-23:59, purely because that row fell one day before the range
# began. Reaching back a month lets a corrected reader repair recent history rather
# than leave it stranded.
AVAILABILITY_SYNC_BACKFILL_DAYS = 30


@login_required
def employees(request):
    """The roster and everyone's usual weekly availability on one page.

    These were two separate pages, which meant answering "can this person work a
    Thursday?" required holding two screens in your head. Availability is stored per
    date, so the weekly pattern is derived: for each weekday, the window an employee
    most commonly holds. That is how Square records it and how management think about
    it - "Kate does Wednesday evenings" - rather than as several hundred dated rows.

    The button posts back here and re-reads Square directly, because this is the page
    where a wrong window is noticed and it should be the page where it is fixed.
    """
    if request.method == "POST":
        if request.POST.get("action") == "connect":
            return _connect_to_square(request)
        return _sync_availability_from_square(request)

    WEEKDAYS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")

    departments = list(Department.objects.all())
    selected_department = (request.GET.get("department") or "").strip()
    staff_query = Employee.objects.prefetch_related(
        "employee_roles__role", "department_memberships__department"
    ).order_by("display_name")
    if selected_department:
        staff_query = staff_query.filter(
            department_memberships__department__name=selected_department
        )
    staff = list(staff_query.distinct())

    # The most recent entry for each weekday, not the most frequent one. Availability
    # is stored per date and only the dates a sync covers get refreshed, so history
    # holds superseded values - Kate's Thursday was recorded as 05:30 before that
    # transcription was corrected to 17:30. Taking the majority reinstates whichever
    # version happens to appear most often; taking the latest reflects what is
    # currently on file, and a change shows up immediately rather than once it has
    # outnumbered the old value.
    def describe(entry) -> str:
        if entry.availability_type == AvailabilityType.AVAILABLE_ALL_DAY:
            return "All day"
        if entry.availability_type == AvailabilityType.AVAILABLE_WINDOW:
            if entry.start_time and entry.end_time:
                return f"{entry.start_time:%H:%M}-{entry.end_time:%H:%M}"
            return ""
        if entry.availability_type == AvailabilityType.UNAVAILABLE:
            return "Unavailable"
        return ""  # UNKNOWN: nothing on file for that day

    # Which row wins for a given weekday, in order of authority:
    #
    #   1. anything a person typed in    - the only availability on file for staff
    #                                      Square knows nothing about; a sync must
    #                                      never erase or override it
    #   2. a real Square dashboard read
    #   3. the fixture stand-in          - hand-transcribed hours used only when no
    #                                      dashboard session exists
    #   4. a retired provider            - ignored outright
    #
    # Ordering by availability *date* was the bug behind seventeen staff showing "All
    # day" every Monday and Sunday. The retired feed wrote rows dated September; the
    # current sync writes rows through December. Sorting by date therefore let a row
    # written months ago outrank one written minutes ago, because its date was
    # further in the future. What settles which value is current is when the row was
    # *written*, which is updated_at.
    from scheduling.integrations.square_availability.service import (
        FALLBACK_AVAILABILITY_SOURCE,
        RETIRED_AVAILABILITY_SOURCES,
        SQUARE_AVAILABILITY_SOURCE,
    )

    def authority(entry) -> int:
        if entry.source == SQUARE_AVAILABILITY_SOURCE:
            return 2
        if entry.source == FALLBACK_AVAILABILITY_SOURCE:
            return 1
        return 3  # entered by hand

    entries = list(
        EmployeeAvailability.objects.filter(employee__in=staff)
        .exclude(source__in=RETIRED_AVAILABILITY_SOURCES)
        .order_by("date")
    )

    winners: dict[tuple[int, int], tuple[int, datetime, date]] = {}
    for entry in entries:
        key = (entry.employee_id, entry.date.weekday())
        rank = (authority(entry), entry.updated_at, entry.date)
        if key not in winners or rank > winners[key]:
            winners[key] = rank

    # The winning rank only settles *which* source and date to believe, and then every
    # row on that date is kept. Ranking row against row would break the case this page
    # exists to show: two windows on one weekday are written by the same sync a
    # microsecond apart, so the later write would evict the earlier one and Khrystyna
    # would lose the evening that lets her work a show at all.
    windows: dict[int, dict[int, list[str]]] = {e.id: {} for e in staff}
    for entry in entries:
        key = (entry.employee_id, entry.date.weekday())
        winner = winners.get(key)
        if winner is None or (authority(entry), entry.date) != (winner[0], winner[2]):
            continue
        held = windows[entry.employee_id].setdefault(entry.date.weekday(), [])
        text = describe(entry)
        if text and text not in held:
            held.append(text)

    live_sources = set(
        EmployeeAvailability.objects.filter(employee__in=staff)
        .exclude(source__in=RETIRED_AVAILABILITY_SOURCES)
        .values_list("source", flat=True)
        .distinct()
    )
    mapped_ids = set(
        SquareEmployeeMapping.objects.filter(environment="production").values_list(
            "employee_id", flat=True
        )
    )

    rows = []
    for employee in staff:
        pattern = []
        known = 0
        for index, label in enumerate(WEEKDAYS):
            texts = sorted(windows[employee.id].get(index) or [])
            if texts:
                known += 1
            pattern.append(
                {
                    "day": label,
                    "texts": texts,
                    "unavailable": texts == ["Unavailable"],
                    "blank": not texts,
                }
            )
        rows.append(
            {
                "employee": employee,
                "pattern": pattern,
                "days_known": known,
                "roles": list(employee.employee_roles.all()),
                "square_mapped": employee.id in mapped_ids,
                "departments": [
                    m.department.name for m in employee.department_memberships.all()
                ],
            }
        )

    # The same person genuinely belongs to several departments, so a person appears
    # under each one they are in. That is the staff list management keep, not
    # duplication to be tidied away.
    rows_by_employee = {r["employee"].id: r for r in rows}
    grouped = []
    for department in departments:
        if selected_department and department.name != selected_department:
            continue
        members = [
            rows_by_employee[m.employee_id]
            for m in department.memberships.select_related("employee").all()
            if m.employee_id in rows_by_employee
        ]
        if members:
            grouped.append({"department": department, "rows": members})
    placed = {m.employee_id for d in departments for m in d.memberships.all()}
    unplaced = [r for r in rows if r["employee"].id not in placed]
    if unplaced and not selected_department:
        grouped.append({"department": None, "rows": unplaced})

    from scheduling.integrations.square_session import session_status

    last_sync = SquareAvailabilitySyncRun.objects.order_by("-started_at").first()
    session = session_status()

    return render(
        request,
        "scheduling/employees.html",
        {
            "rows": rows,
            "grouped": grouped,
            "departments": departments,
            "selected_department": selected_department,
            "weekdays": WEEKDAYS,
            "no_availability": [r["employee"].display_name for r in rows if not r["days_known"]],
            "session_connected": session.connected,
            "session_expired": session.expired,
            "session_detail": session.detail,
            "last_sync": last_sync,
            # Says out loud when the hours on screen are a stand-in rather than
            # Square's own answer. That distinction was invisible before, and its
            # invisibility is what let transcribed hours pass as live ones.
            "using_fallback": FALLBACK_AVAILABILITY_SOURCE in live_sources,
            "sync_days": AVAILABILITY_SYNC_DAYS,
            "backfill_days": AVAILABILITY_SYNC_BACKFILL_DAYS,
            "time_off_form": TimeOffForm(),
            # Absences that can still affect a roster. Anything already finished is
            # history, and only clutters the page management plans from.
            "time_off": (
                EmployeeTimeOff.objects.filter(end_date__gte=date.today())
                .select_related("employee")
                .order_by("start_date", "employee__display_name")
            ),
        },
    )


def _connect_to_square(request):
    """Sign in to Square from inside the application.

    This used to be a Terminal command, which meant the one thing that goes wrong on
    its own schedule - Square expiring the session - could only be fixed by leaving
    the application. The window that opens is Square's own login page; the password
    is typed there and never passes through this application.
    """
    from scheduling.services.square_pull import SquarePullError, run_square_connect

    try:
        result = run_square_connect()
    except SquarePullError as exc:
        messages.error(request, f"Could not sign in to Square: {exc}")
        return redirect("employees")

    messages.success(
        request,
        f"Connected to Square. {result.get('detail', '')} "
        "Press Sync availability from Square to read the current hours.",
    )
    return redirect("employees")


def _sync_availability_from_square(request):
    """Read the availability grid from Square and replace what the sync owns.

    The read runs in its own process. Playwright drives browsers through asyncio
    subprocesses, which on Unix need a process's main thread; called from a request
    thread the interpreter dies outright and takes the application with it.
    """
    from scheduling.services.square_pull import SquarePullError, run_availability_sync

    start = date.today() - timedelta(days=AVAILABILITY_SYNC_BACKFILL_DAYS)
    end = date.today() + timedelta(days=AVAILABILITY_SYNC_DAYS)

    try:
        result = run_availability_sync(start, end)
    except SquarePullError as exc:
        # An expired sign-in is the one failure with a specific remedy, so record it
        # rather than leaving the page claiming a live connection until the next
        # attempt fails the same way.
        if "expired" in str(exc).lower() or "sign-in" in str(exc).lower():
            from scheduling.integrations.square_session import mark_session_expired

            mark_session_expired()
            messages.error(
                request,
                "Square signed this application out. Press Connect to Square, sign in "
                "in the window that opens, then sync again.",
            )
        else:
            messages.error(request, f"Could not read availability from Square: {exc}")
        return redirect("employees")

    if not result.get("live"):
        messages.warning(
            request,
            "Square was not read. These hours come from the built-in fallback, which "
            "is transcribed by hand and out of date. Connect the Square dashboard "
            "(manage.py square_connect) and sync again.",
        )
        return redirect("employees")

    detail = (
        f"Availability synced from Square: {result['known']} of {result['total']} "
        f"employee/date entries known ({result['completeness']}%), "
        f"{start:%d %b} to {end:%d %b %Y}."
    )
    unmatched = result.get("unmatched") or []
    if unmatched:
        detail += (
            f" {len(unmatched)} name(s) in Square matched nobody on the roster: "
            f"{', '.join(unmatched[:6])}."
        )
    messages.success(request, detail)
    return redirect("employees")


@login_required
def roles(request):
    role_list = Role.objects.annotate(
        active_employee_count=Count(
            "employee_roles",
            filter=Q(employee_roles__active=True, employee_roles__employee__active=True),
        )
    )
    return render(request, "scheduling/roles.html", {"roles": role_list})


def settings_file_path():
    """Where the Square credentials live: the app's data folder, or the project .env.

    The packaged app points SPIRIT_SETTINGS_FILE at its Application Support folder,
    because its own directory is read-only once installed.
    """
    from pathlib import Path

    configured = os.getenv("SPIRIT_SETTINGS_FILE")
    if configured:
        return Path(configured)
    return Path(__file__).resolve().parent.parent / ".env"


def _write_setting(name: str, value: str) -> None:
    """Set one key in the settings file, leaving every other line untouched."""
    path = settings_file_path()
    lines = path.read_text().splitlines() if path.exists() else []
    replaced = False
    for index, line in enumerate(lines):
        if line.strip().startswith(f"{name}="):
            lines[index] = f"{name}={value}"
            replaced = True
            break
    if not replaced:
        lines.append(f"{name}={value}")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n")
    path.chmod(0o600)  # credentials: readable only by this user
    os.environ[name] = value


@login_required
def square_integration(request):
    """Square connection status, and where the access token is entered.

    The token used to be settable only by hand-editing a file inside the application
    folder, which is not a reasonable thing to ask and is impossible once the app is
    installed and read-only. It is written to the settings file with owner-only
    permissions and never displayed back in full.
    """
    if request.method == "POST":
        action = request.POST.get("action")
        if action == "save_token":
            token = (request.POST.get("access_token") or "").strip()
            environment = request.POST.get("environment", "production").strip().lower()
            if environment not in {"sandbox", "production"}:
                environment = "production"
            if not token:
                messages.error(request, "Paste an access token before saving.")
            else:
                key = (
                    "SQUARE_PRODUCTION_ACCESS_TOKEN"
                    if environment == "production"
                    else "SQUARE_SANDBOX_ACCESS_TOKEN"
                )
                _write_setting(key, token)
                _write_setting("SQUARE_ENVIRONMENT", environment)
                messages.success(
                    request,
                    f"Saved. Testing the {environment} connection now - the result is below.",
                )
        elif action == "clear_token":
            _write_setting("SQUARE_PRODUCTION_ACCESS_TOKEN", "")
            messages.success(request, "Production access token removed from this Mac.")
        return redirect("square_integration")

    context = square_connection_context()
    config = None
    try:
        config = SquareConfig.from_env()
    except SquareIntegrationError:
        pass

    token = (getattr(config, "production_access_token", "") or "") if config else ""
    context.update(
        {
            "has_token": bool(token),
            # Enough to confirm which token is loaded, never enough to reuse it.
            "token_hint": f"...{token[-4:]}" if len(token) >= 4 else "",
            "settings_path": str(settings_file_path()),
            "selected_environment": (
                config.environment.value if config else "production"
            ),
            "location_id": getattr(config, "location_id", "") if config else "",
        }
    )
    return render(request, "scheduling/square_integration.html", context)


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

    # The show calendar is rendered entirely in the browser - none of the event titles
    # exist in the page HTML - so it has to be read through a real browser engine, in a
    # separate process. See scheduling.services.calendar_import for why.
    try:
        result = run_calendar_import(start, end)
    except CalendarImportError as exc:
        logger.warning("Live calendar import failed: %s", exc)
        messages.error(request, f"The live calendar could not be read: {exc}")
        return redirect("show_list")

    note = (
        f"Imported {start:%d %b %Y} to {end:%d %b %Y} from the live calendar: "
        f"{result.received} show(s) found - {result.created} added, "
        f"{result.updated} updated"
        + (f", {result.unchanged} unchanged" if result.unchanged else "")
        + "."
    )
    if result.is_partial:
        messages.warning(
            request,
            f"{note} The calendar showed {result.rendered} events but only "
            f"{result.extracted} could be read, so some may be missing. {result.notes}",
        )
    else:
        messages.success(request, note)

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
    """Schedules grouped by what has actually happened to them.

    A flat list of every run gave no way to tell a roster already sitting in Square
    from a half-finished draft or something long superseded - they all looked alike,
    and there have been enough runs for the same dates that picking the live one
    mattered.

    square_shift_count is what the row's buttons key off, rather than the section.
    A pilot sync puts a single shift in Square while the run is still only Approved,
    and a superseded run can have a full roster still sitting there - neither lands
    in the "In Square" section, so keying the remove button off the section left both
    with shifts in Square and no way to remove them.
    """
    today = date.today()
    runs = list(
        ScheduleRun.objects.select_related("created_by", "approved_by")
        .annotate(assignment_count=Count("assignments", distinct=True))
        .order_by("-start_date", "-id")
    )
    still_in_square = shifts_still_in_square_by_run([run.pk for run in runs])
    for run in runs:
        run.square_shift_count = len(still_in_square.get(run.pk, ()))

    groups = {"posted": [], "in_progress": [], "past": [], "superseded": []}
    for run in runs:
        upcoming = run.end_date >= today
        if run.status == ScheduleRunStatus.SUPERSEDED_SOURCE_DATA:
            key = "superseded"
        elif run.status == ScheduleRunStatus.SYNCED_TO_SQUARE:
            key = "posted" if upcoming else "past"
        elif not upcoming:
            key = "past"
        else:
            key = "in_progress"
        groups[key].append(run)

    sections = [
        {
            "key": "posted",
            "title": "In Square",
            "blurb": "Sent to Square as drafts. Publishing to staff stays a manual step there.",
            "tone": "success",
            "runs": groups["posted"],
        },
        {
            "key": "in_progress",
            "title": "In progress",
            "blurb": "Upcoming periods still being built, reviewed or approved.",
            "tone": "primary",
            "runs": groups["in_progress"],
        },
        {
            "key": "past",
            "title": "Past periods",
            "blurb": "Finished periods, kept for the fairness history they feed.",
            "tone": "secondary",
            "runs": groups["past"],
        },
        {
            "key": "superseded",
            "title": "Superseded",
            "blurb": "Replaced by a later run for the same dates. Never sent anywhere.",
            "tone": "light",
            "runs": groups["superseded"],
        },
    ]

    return render(
        request,
        "scheduling/schedule_list.html",
        {
            "sections": [s for s in sections if s["runs"]],
            "total": len(runs),
            "today": today,
        },
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
        "server-manager",
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
    # Where the shows in this run actually came from. Without this the page fell back
    # to a flat "DEMO / SEED" badge whenever no snapshot was linked, which reads as
    # "these shows are made up" even when every one of them was imported from the live
    # site. The snapshot records the pull; the shows record their own origin.
    imported_shows = Show.objects.filter(
        date__range=(schedule_run.start_date, schedule_run.end_date),
        active=True,
        source=Show.Source.CALENDAR_IMPORT,
    )
    calendar_origin = (
        imported_shows.exclude(source_url="").values_list("source_url", flat=True).first()
    )
    return render(
        request,
        "scheduling/schedule_detail.html",
        {
            "schedule_run": schedule_run,
            "imported_show_count": imported_shows.count(),
            "calendar_origin": calendar_origin,
            "schedule_rows": _schedule_rows(schedule_run),
            "metrics": metrics,
            "hard_errors": schedule_run.warnings.filter(
                severity=WarningSeverity.ERROR, resolved=False
            ).count(),
            # Only the warnings that actually block approval, so the manager can see
            # what is in the way and clear it. The general warnings list was removed
            # from this page deliberately; this is not that list coming back - it is
            # empty on a clean run and never shows the informational ones.
            "blocking_warnings": (
                schedule_run.warnings.filter(severity=WarningSeverity.ERROR, resolved=False)
                .select_related("show")
                .order_by("show__date", "warning_type")
            ),
            # Surfaced separately, above everything. An overlap explains most or all of
            # the shortages on the page at once, and reading it off forty-six identical
            # per-position warnings is not a reasonable thing to ask of anyone.
            "overlapping_rosters": schedule_run.warnings.filter(
                warning_type=WarningType.OVERLAPPING_ROSTER, resolved=False
            ),
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
                start_time=form.cleaned_data["start_time"],
                end_time=form.cleaned_data["end_time"],
                swap=form.cleaned_data["swap"],
            )
            assignment.refresh_from_db()
            messages.success(
                request,
                f"{assignment.shift_template.name} is now {assignment.employee.display_name}."
                + (
                    " Their positions were swapped."
                    if form.cleaned_data["swap"]
                    else " Recorded with an audit reason."
                ),
            )
            return redirect("schedule_detail", run_id=assignment.schedule_run_id)
        except ValidationError as exc:
            form.add_error(None, exc)
    return render(
        request,
        "scheduling/schedule_override.html",
        {
            "assignment": assignment,
            "form": form,
            # Named so the page can say who a swap is even possible with.
            "swappable_rows": [
                {"name": employee.display_name, "slot": form.swappable[employee.id]}
                for employee in form.fields["employee"].queryset
                if employee.id in form.swappable
            ],
        },
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
def schedule_delete(request, run_id):
    """Delete a run outright, together with the roster it produced.

    Everything else here is deliberately soft - shows deactivate, runs supersede -
    because the history feeds fairness tracking. This one is a real delete: a
    half-built draft for a period still being worked on is clutter, not history.

    Anything that reached Square is refused. The drafts sitting in Square would
    outlive the local record that explains where they came from, and the audit log
    proving what was sent would go with it.

    A run that is already gone is not an error. The browser replays this POST on a
    back-navigation or a reload, and a second tab can still be showing the run in a
    list rendered before it went - all of which used to raise a 404 and dump a debug
    page over a delete that had in fact succeeded.
    """
    schedule_run = ScheduleRun.objects.filter(pk=run_id).first()
    if schedule_run is None:
        messages.info(request, f"Schedule #{run_id} has already been deleted.")
        return redirect("schedule_list")
    if request.method != "POST":
        return redirect("schedule_detail", run_id=run_id)

    if schedule_run.status == ScheduleRunStatus.SYNCED_TO_SQUARE:
        messages.error(
            request,
            f"Schedule #{schedule_run.pk} has been sent to Square. "
            "Remove its drafts in Square before deleting it here.",
        )
        return redirect("schedule_list")

    # Ask what is still in Square, not what was ever put there. A preview creates
    # nothing, and a shift already removed is no longer a reason to refuse - gating
    # on "has ever created" left a run permanently undeletable even after every one
    # of its shifts had been taken out of Square.
    remaining = shifts_still_in_square(schedule_run)
    if remaining:
        messages.error(
            request,
            f"Schedule #{schedule_run.pk} still has {len(remaining)} "
            f"shift{pluralize(len(remaining))} in Square. Use Remove from Square on "
            "this row first, then delete it.",
        )
        return redirect("schedule_list")
    if has_untracked_square_creations(schedule_run):
        messages.error(
            request,
            f"Schedule #{schedule_run.pk} recorded creating shifts in Square without "
            "keeping their ids, so it cannot be confirmed they are gone. Check Square "
            "and supersede this run rather than deleting the only record of it.",
        )
        return redirect("schedule_list")

    shifts = schedule_run.assignments.count()
    warnings = schedule_run.warnings.count()
    period = f"{schedule_run.start_date:%d %b %Y} - {schedule_run.end_date:%d %b %Y}"
    schedule_run.delete()
    messages.success(
        request,
        f"Deleted schedule #{run_id} ({period}) along with "
        f"{shifts} shift{pluralize(shifts)} and {warnings} warning{pluralize(warnings)}.",
    )
    return redirect("schedule_list")


@login_required
def schedule_square_remove(request, run_id):
    """Delete this run's drafts out of Square itself, permanently.

    The only outward-destructive action in the application. Everything else that
    touches Square creates drafts and leaves publishing to a manager; this reaches
    into the live account and removes shifts that are already there.

    It deletes only the shift ids the audit log records this application as having
    created, so a shift a manager added by hand is never touched. Square has no
    delete for a published shift that this integration is willing to perform, so
    any that a manager has already published are reported back rather than left
    half-removed.
    """
    schedule_run = ScheduleRun.objects.filter(pk=run_id).first()
    if schedule_run is None:
        # Same replayed-POST case as schedule_delete. Doubly worth catching here:
        # a resubmit that raised a 404 would look like the Square removal failed,
        # when the first one may well have deleted the shifts.
        messages.info(request, f"Schedule #{run_id} no longer exists here.")
        return redirect("schedule_list")
    if request.method != "POST":
        return redirect("schedule_detail", run_id=run_id)

    try:
        result = remove_run_from_square(schedule_run, request.user)
    except SquareProductionWritesDisabledError:
        messages.error(
            request,
            "Removing shifts from Square needs SQUARE_PRODUCTION_WRITES_ENABLED=true "
            "in .env. It is off, so nothing was touched.",
        )
        return redirect("schedule_list")
    except SquareIntegrationError as exc:
        messages.error(request, f"Square refused the removal: {exc}. Nothing else was changed.")
        return redirect("schedule_list")

    removed = result.deleted + result.already_gone
    messages.warning(
        request,
        f"Permanently deleted {removed} shift{pluralize(removed)} from Square for "
        f"schedule #{schedule_run.pk}. This cannot be undone - Square keeps no copy, "
        "and re-sending the roster would create new shifts.",
    )
    if result.published:
        messages.error(
            request,
            f"{len(result.published)} shift{pluralize(len(result.published))} "
            "had already been published to staff in Square and must be deleted there by "
            "a manager. This application never publishes, so it cannot remove them.",
        )
    if result.failed:
        messages.error(
            request,
            f"{len(result.failed)} shift{pluralize(len(result.failed))} could not be "
            "removed and are still in Square. The Square sync log on the schedule has "
            "the reason for each.",
        )
    return redirect("schedule_list")


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

    # Whether anything from this run is already sitting in Square. If it is, the page
    # offers to reconcile rather than only to create, which is what a second visit is
    # almost always for.
    # Assignments that have a shift in Square, not every shift id ever written: a run
    # re-synced a few times accumulates historical ids, and counting those said "111
    # shifts" for a schedule of 49.
    square_shift_count = (
        SquareSyncAuditLog.objects.filter(
            schedule_run=schedule_run,
            assignment__isnull=False,
            action_type__in=(
                SquareSyncAuditAction.PRODUCTION_DRAFT_CREATED,
                SquareSyncAuditAction.PRODUCTION_PILOT_CREATED,
            ),
        )
        .exclude(square_scheduled_shift_id="")
        .values("assignment_id")
        .distinct()
        .count()
    )

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
            "already_in_square": square_shift_count > 0,
            "square_shift_count": square_shift_count,
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
def square_production_update(request, run_id):
    """Push changes made after the first sync into Square.

    Separate from the first bulk create, which stays behind its typed confirmation:
    that one puts a whole roster into a live account for the first time, this one
    reconciles a roster already there with the corrections made since.
    """
    schedule_run = get_object_or_404(ScheduleRun, pk=run_id)
    if request.method == "POST":
        try:
            result = update_run_in_square(schedule_run, user=request.user)
        except SquareProductionWritesDisabledError:
            messages.error(
                request,
                "Updating Square needs SQUARE_PRODUCTION_WRITES_ENABLED=true in the "
                "settings file. It is off, so nothing was touched.",
            )
            return redirect("square_production_sync_hub", run_id=run_id)
        except (SquareProductionSyncError, SquareIntegrationError) as exc:
            messages.error(request, f"Square refused the update: {exc}")
            return redirect("square_production_sync_hub", run_id=run_id)

        if result.changed_anything:
            parts = []
            if result.updated:
                parts.append(f"{result.updated} shift{pluralize(result.updated)} updated")
            if result.created:
                parts.append(f"{result.created} added")
            if result.deleted:
                parts.append(f"{result.deleted} removed")
            messages.success(
                request,
                "Square now matches this schedule: "
                + ", ".join(parts)
                + f". {result.unchanged} were already correct.",
            )
        else:
            messages.info(
                request,
                f"Square already matches this schedule; nothing needed changing "
                f"({result.unchanged} shift{pluralize(result.unchanged)} checked).",
            )

        # Published shifts are the one thing this cannot rewrite: staff have already
        # been told those hours, so they are named rather than silently skipped.
        if result.published_blocked:
            messages.warning(
                request,
                "Left unchanged because they are already published to staff in Square: "
                + "; ".join(result.published_blocked)
                + ". Change these in the Square dashboard, or unpublish them there first.",
            )
        if result.failed:
            messages.error(request, "Square rejected: " + "; ".join(result.failed))
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


@login_required
def schedule_square_compare(request, run_id):
    """Read Square's version of this roster and show where it has diverged.

    Management edit rosters directly in Square - swapping a bartender, shifting a
    start, adding someone the engine never considered. Those decisions were previously
    invisible here, so the local run stopped describing reality and the next generated
    schedule would quietly undo them.
    """
    schedule_run = get_object_or_404(ScheduleRun, pk=run_id)

    if request.method == "POST" and request.POST.get("action") == "adopt":
        try:
            report = compare_run_with_square(schedule_run)
            applied = adopt_square_version(schedule_run, report, request.user)
        except SquareReadError as exc:
            messages.error(request, f"Square could not be read: {exc}")
            return redirect("schedule_square_compare", run_id=run_id)
        except ValidationError as exc:
            messages.error(request, f"Square's version could not be applied: {exc}")
            return redirect("schedule_square_compare", run_id=run_id)

        parts = [
            f"{applied['updated']} updated" if applied["updated"] else "",
            f"{applied['added']} added" if applied["added"] else "",
            f"{applied['removed']} removed" if applied["removed"] else "",
        ]
        summary = ", ".join(p for p in parts if p) or "nothing to change"
        messages.success(request, f"This schedule now matches Square: {summary}.")
        for note in applied["skipped"]:
            messages.warning(request, f"Not applied - {note}")
        return redirect("schedule_square_compare", run_id=run_id)

    report = None
    error = ""
    try:
        report = compare_run_with_square(schedule_run)
    except SquareReadError as exc:
        error = str(exc)

    return render(
        request,
        "scheduling/schedule_square_compare.html",
        {
            "schedule_run": schedule_run,
            "report": report,
            "error": error,
            "added": report.of_kind("ADDED_IN_SQUARE") if report else [],
            "removed": report.of_kind("REMOVED_FROM_SQUARE") if report else [],
            "edited": report.of_kind("EDITED_IN_SQUARE") if report else [],
            "unmapped": report.of_kind("UNMAPPED") if report else [],
        },
    )


@login_required
def square_pull(request):
    """One action that refreshes everything Square knows.

    The show calendar, staff availability and the rosters Square holds were three
    separate chores in three separate places. Each still runs as its own process -
    browser automation cannot run in a web request thread without taking the
    application down with it - but they are triggered together from here.
    """
    from scheduling.integrations.square_session import session_status
    from scheduling.services.square_pull import pull_everything

    default_start = date.today()
    default_end = default_start + timedelta(days=60)

    if request.method == "POST":
        start = _parse_optional_date(request.POST.get("start")) or default_start
        end = _parse_optional_date(request.POST.get("end")) or default_end
        if end < start:
            start, end = end, start

        report = pull_everything(start, end)
        for step in report.steps:
            if not step.ok:
                messages.error(request, f"{step.name}: {step.detail}")
            elif step.extra.get("partial") or step.extra.get("live") is False:
                messages.warning(request, f"{step.name}: {step.detail}")
            else:
                messages.success(request, f"{step.name}: {step.detail}")
            unmatched = step.extra.get("unmatched") or []
            if unmatched:
                messages.warning(
                    request,
                    f"{len(unmatched)} Square team member(s) are not on the roster, so "
                    f"their availability was ignored: {', '.join(unmatched[:8])}"
                    + ("…" if len(unmatched) > 8 else ""),
                )
        return redirect(f"{reverse('square_pull')}?start={start:%Y-%m-%d}&end={end:%Y-%m-%d}")

    start = _parse_optional_date(request.GET.get("start")) or default_start
    end = _parse_optional_date(request.GET.get("end")) or default_end
    session = session_status()

    runs = ScheduleRun.objects.filter(
        status__in=[ScheduleRunStatus.SYNCED_TO_SQUARE, ScheduleRunStatus.APPROVED]
    ).order_by("-start_date")[:5]

    return render(
        request,
        "scheduling/square_pull.html",
        {
            "start": start.isoformat(),
            "end": end.isoformat(),
            "session_connected": session.connected,
            "session_expired": session.expired,
            "session_detail": session.detail,
            "show_count": Show.objects.filter(active=True, date__range=(start, end)).count(),
            "comparable_runs": runs,
        },
    )


@login_required
def schedule_fill(request, run_id, show_id, code):
    """Staff a position the generator left short."""
    schedule_run = get_object_or_404(ScheduleRun, pk=run_id)
    show = get_object_or_404(Show, pk=show_id)
    template = get_object_or_404(ShiftTemplate, code=code, active=True)
    window = shift_window_for(show, template)

    form = FillAssignmentForm(request.POST or None, template=template, window=window)
    if request.method == "POST" and form.is_valid():
        try:
            fill_assignment(
                schedule_run,
                show,
                template,
                form.cleaned_data["employee"],
                form.cleaned_data["override_reason"],
                start_time=form.cleaned_data["start_time"],
                end_time=form.cleaned_data["end_time"],
            )
            messages.success(
                request,
                f"{form.cleaned_data['employee'].display_name} added to {template.name}.",
            )
            return redirect("schedule_detail", run_id=run_id)
        except ValidationError as exc:
            form.add_error(None, exc)

    return render(
        request,
        "scheduling/schedule_fill.html",
        {
            "schedule_run": schedule_run,
            "show": show,
            "template": template,
            "form": form,
        },
    )


@login_required
def time_off_add(request):
    if request.method == "POST":
        form = TimeOffForm(request.POST)
        if form.is_valid():
            entry = form.save(commit=False)
            entry.source = TimeOffSource.MANUAL
            entry.full_clean()
            entry.save()
            state = "will be" if entry.status == TimeOffStatus.APPROVED else "will not be"
            messages.success(
                request,
                f"Time off recorded for {entry.employee.display_name}. "
                f"It {state} applied when schedules are generated.",
            )
        else:
            messages.error(request, "; ".join(f"{k}: {v[0]}" for k, v in form.errors.items()))
    return redirect("employees")


@login_required
def time_off_approve(request, entry_id):
    entry = EmployeeTimeOff.objects.filter(pk=entry_id).first()
    if entry is None:
        messages.info(request, "That time-off entry no longer exists.")
        return redirect("employees")
    if request.method == "POST":
        entry.status = TimeOffStatus.APPROVED
        entry.save(update_fields=["status"])
        messages.success(
            request,
            f"Approved. {entry.employee.display_name} will not be scheduled "
            f"{entry.start_date:%d %b} to {entry.end_date:%d %b}.",
        )
    return redirect("employees")


@login_required
def time_off_delete(request, entry_id):
    entry = EmployeeTimeOff.objects.filter(pk=entry_id).first()
    if entry is None:
        messages.info(request, "That time-off entry no longer exists.")
        return redirect("employees")
    if request.method == "POST":
        name = entry.employee.display_name
        entry.delete()
        messages.success(request, f"Removed the time-off entry for {name}.")
    return redirect("employees")


@login_required
def time_off_sync(request):
    """Re-read Square's Time off page and mirror it here."""
    if request.method != "POST":
        return redirect("employees")
    from scheduling.integrations.square_session import SquareSessionError
    from scheduling.integrations.square_time_off.service import sync_time_off

    try:
        result = sync_time_off()
    except SquareSessionError as exc:
        messages.error(request, f"{exc}")
        return redirect("employees")
    except Exception as exc:  # the dashboard is a moving target; say so plainly
        logger.exception("time off sync failed")
        messages.error(request, f"Could not read Square's Time off page: {exc}")
        return redirect("employees")

    messages.success(request, f"Time off synced from Square: {result.summary}")
    if result.unmatched:
        messages.warning(
            request,
            "Those names had no match on the roster, so their time off is NOT being "
            "applied. Check the spelling against Square.",
        )
    return redirect("employees")


@login_required
def schedule_warnings_accept_all(request, run_id):
    """Accept every remaining blocker on a run at once, with one shared reason.

    Not every hard error can be solved. A shortage exists precisely because nobody
    eligible could fill it, and on a thin month there can be twenty of them - making
    a manager clear those one at a time is how a safety gate turns into a reason to
    stop using the application.

    The reason is written onto each warning individually, so the audit trail still
    says why each was accepted rather than pointing at a single bulk action.
    """
    schedule_run = ScheduleRun.objects.filter(pk=run_id).first()
    if schedule_run is None:
        messages.info(request, f"Schedule #{run_id} no longer exists.")
        return redirect("schedule_list")
    if request.method != "POST":
        return redirect("schedule_detail", run_id=run_id)

    note = (request.POST.get("resolution_note") or "").strip()
    if len(note) < 5:
        messages.error(request, "A reason of at least five characters is required.")
        return redirect("schedule_detail", run_id=run_id)
    if schedule_run.status in {ScheduleRunStatus.APPROVED, ScheduleRunStatus.SYNCED_TO_SQUARE}:
        messages.error(request, "Warnings on approved schedules cannot be changed.")
        return redirect("schedule_detail", run_id=run_id)

    blocking = schedule_run.warnings.filter(severity=WarningSeverity.ERROR, resolved=False)
    count = blocking.update(resolved=True, resolution_note=note)
    messages.success(
        request,
        f"Accepted {count} blocker{pluralize(count)} on schedule #{run_id}. "
        "It can be approved now, and the reason is recorded against each one.",
    )
    return redirect("schedule_detail", run_id=run_id)


def password_reset(request):
    """Ask for a reset link. Deliberately outside @login_required.

    A typo is told it is a typo, and a real account is told which inbox the link went
    to. That does let someone probe which names exist, and on a public site it would be
    wrong - but this application is bound to 127.0.0.1 on a single Mac, so anyone who
    can load this page can already read the database beside it. Trading a real
    usability win against an attacker who is by definition already inside is not a
    trade worth making the other way.
    """
    from django.conf import settings

    from scheduling.services.password_reset import (
        build_link,
        email_link,
        find_user,
        write_link_to_disk,
    )

    context = {"email_configured": getattr(settings, "EMAIL_IS_CONFIGURED", False)}

    if request.method == "POST":
        identifier = (request.POST.get("identifier") or "").strip()
        user = find_user(identifier)

        if user is None:
            messages.error(
                request,
                f"No active account matches “{identifier}”. Check the spelling, or try "
                "the other of your username and email address.",
            )
            return redirect("password_reset")

        link = build_link(user, request)
        sent, detail = email_link(user, link)
        if sent:
            messages.success(request, f"A reset link has been sent to {detail}.")
            return redirect("password_reset")

        try:
            path = write_link_to_disk(link)
        except OSError as exc:
            messages.error(request, f"The reset link could not be saved ({exc}).")
            return redirect("password_reset")
        messages.warning(
            request,
            f"{detail} The link was saved to {path} instead - open that file and "
            f"follow the link inside.",
        )
        return redirect("password_reset")

    return render(request, "registration/password_reset.html", context)


def password_reset_confirm(request, uidb64, token):
    """Set the new password, for someone arriving from the link.

    The token is Django's own: tied to the account's current password hash and last
    login, so it stops working the moment the password changes, and it expires on its
    own. Checked before the form is even shown, so a dead link says so rather than
    taking a password and then refusing it.
    """
    from django.contrib.auth import get_user_model
    from django.contrib.auth.password_validation import (
        password_validators_help_texts,
        validate_password,
    )
    from django.contrib.auth.tokens import default_token_generator
    from django.utils.encoding import force_str
    from django.utils.http import urlsafe_base64_decode

    try:
        user = get_user_model().objects.get(pk=force_str(urlsafe_base64_decode(uidb64)))
    except (TypeError, ValueError, OverflowError, get_user_model().DoesNotExist):
        user = None

    if user is None or not default_token_generator.check_token(user, token):
        return render(request, "registration/password_reset_invalid.html", status=400)

    context = {"username": user.username, "password_rules": password_validators_help_texts()}

    if request.method == "POST":
        new = request.POST.get("new_password") or ""
        again = request.POST.get("confirm_password") or ""
        if new != again:
            messages.error(request, "The two passwords do not match.")
            return render(request, "registration/password_reset_confirm.html", context)
        try:
            validate_password(new, user)
        except ValidationError as exc:
            messages.error(request, " ".join(exc.messages))
            return render(request, "registration/password_reset_confirm.html", context)

        user.set_password(new)
        user.save()
        messages.success(request, "Your password has been changed. Sign in with it now.")
        return redirect("login")

    return render(request, "registration/password_reset_confirm.html", context)


@login_required
def management_users(request):
    """Who can sign in, and how a colleague is given an account.

    There is deliberately no public sign-up. This page is bound to 127.0.0.1 but the
    accounts it creates reach real staff data and a live Square connection, so an open
    registration form would let anyone who can load the page mint themselves a manager.
    Accounts are made here, by someone already signed in.

    The inviter never chooses the password. Handing someone one to "change later" means
    it gets written down, shared over something, and usually never changed; the link
    lets them pick their own and means nobody else ever knew it.
    """
    from django.contrib.auth import get_user_model

    from scheduling.services.password_reset import build_link, email_invitation

    users = get_user_model().objects.order_by("-is_active", "username")

    if request.method == "POST" and request.POST.get("action") == "invite":
        username = (request.POST.get("username") or "").strip()
        email = (request.POST.get("email") or "").strip()

        if not username or not email:
            messages.error(request, "A username and an email address are both needed.")
        elif get_user_model().objects.filter(username__iexact=username).exists():
            messages.error(request, f"There is already an account called “{username}”.")
        elif get_user_model().objects.filter(email__iexact=email).exists():
            messages.error(request, f"{email} is already on another account.")
        else:
            new_user = get_user_model().objects.create(
                username=username, email=email, is_staff=False, is_superuser=False
            )
            # No password is set at all, so the account cannot be signed into until
            # its owner follows the link and chooses one.
            new_user.set_unusable_password()
            new_user.save()

            link = build_link(new_user, request)
            sent, detail = email_invitation(new_user, link, request.user.get_username())
            if sent:
                messages.success(
                    request, f"“{username}” has been invited. The link went to {detail}."
                )
            else:
                messages.warning(
                    request,
                    f"“{username}” was created, but {detail} Send them this link "
                    f"yourself - it can be used once: {link}",
                )
        return redirect("management_users")

    if request.method == "POST" and request.POST.get("action") == "approve":
        target = get_user_model().objects.filter(pk=request.POST.get("user_id")).first()
        if target is None:
            messages.error(request, "That account no longer exists.")
        else:
            target.is_active = True
            target.save()
            messages.success(request, f"“{target.username}” can now sign in.")
        return redirect("management_users")

    if request.method == "POST" and request.POST.get("action") == "reject":
        target = get_user_model().objects.filter(pk=request.POST.get("user_id")).first()
        if target is None:
            messages.error(request, "That account no longer exists.")
        elif target.last_login is not None:
            # Only ever removes an account that has never been used. Anything that has
            # signed in has history worth keeping; that one gets disabled, not deleted.
            messages.error(
                request,
                f"“{target.username}” has signed in before, so it can be disabled but "
                "not deleted.",
            )
        else:
            name = target.username
            target.delete()
            messages.success(request, f"The request from “{name}” has been removed.")
        return redirect("management_users")

    if request.method == "POST" and request.POST.get("action") == "deactivate":
        target = get_user_model().objects.filter(pk=request.POST.get("user_id")).first()
        if target is None:
            messages.error(request, "That account no longer exists.")
        elif target.pk == request.user.pk:
            # Otherwise one careless click locks the last person out of their own app.
            messages.error(request, "You cannot deactivate the account you are signed in with.")
        elif get_user_model().objects.filter(is_active=True).count() <= 1:
            messages.error(request, "This is the only account that can sign in.")
        else:
            target.is_active = False
            target.save()
            messages.success(request, f"“{target.username}” can no longer sign in.")
        return redirect("management_users")

    return render(
        request,
        "scheduling/management_users.html",
        {
            "users": users.filter(is_active=True),
            "disabled_users": users.filter(is_active=False, last_login__isnull=False),
            # Never signed in and not yet active: someone who used the sign-up link.
            "pending_users": users.filter(is_active=False, last_login__isnull=True),
            "email_configured": getattr(settings, "EMAIL_IS_CONFIGURED", False),
            "registration_open": not getattr(
                settings, "REGISTRATION_REQUIRES_APPROVAL", True
            ),
        },
    )


def register(request):
    """Create an account. Outside @login_required - that is the whole point.

    New accounts are held inactive until a manager approves them, unless
    REGISTRATION_REQUIRES_APPROVAL is turned off. The link is public and this
    application reaches staff records and a live Square connection, so "anyone who can
    load the page gets in" is not a default worth shipping - but it is one setting away
    for whoever wants it.

    The person chooses their own password here, validated by Django's own rules.
    """
    from django.contrib.auth import get_user_model
    from django.contrib.auth.password_validation import (
        password_validators_help_texts,
        validate_password,
    )

    needs_approval = getattr(settings, "REGISTRATION_REQUIRES_APPROVAL", True)
    context = {
        "password_rules": password_validators_help_texts(),
        "needs_approval": needs_approval,
    }

    if request.method != "POST":
        return render(request, "registration/register.html", context)

    username = (request.POST.get("username") or "").strip()
    email = (request.POST.get("email") or "").strip()
    password = request.POST.get("password") or ""
    again = request.POST.get("confirm_password") or ""
    users = get_user_model().objects

    if not username or not email:
        messages.error(request, "A username and an email address are both needed.")
    elif users.filter(username__iexact=username).exists():
        messages.error(request, f"There is already an account called “{username}”.")
    elif users.filter(email__iexact=email).exists():
        messages.error(request, f"{email} is already on another account.")
    elif password != again:
        messages.error(request, "The two passwords do not match.")
    else:
        try:
            validate_password(password)
        except ValidationError as exc:
            messages.error(request, " ".join(exc.messages))
        else:
            person = users.create_user(
                username=username,
                email=email,
                password=password,
                is_active=not needs_approval,
            )
            if needs_approval:
                messages.success(
                    request,
                    f"Thanks — the account “{person.username}” has been created and is "
                    "waiting for a manager to approve it. You will be able to sign in "
                    "once they have.",
                )
            else:
                messages.success(request, "Your account is ready. Sign in below.")
            return redirect("login")

    return render(request, "registration/register.html", context)
