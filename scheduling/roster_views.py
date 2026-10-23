"""Build a roster for one show: pick it, confirm the guests, see who is free, generate.

Steps 1-4 of the auto-roster flow. Steps 5-6 (review, replace, approve) are the
existing schedule pages, and step 7 is the existing Square draft sync - this module
hands over to them rather than duplicating them.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import ValidationError
from django.db import transaction
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone

from scheduling.forms import (
    FLOOR_ROLE_NAMES,
    AvailabilityOverrideForm,
    GuestCountForm,
    floor_staff,
)
from scheduling.models import (
    MAX_THEATRE_CAPACITY,
    AvailabilityOverride,
    AvailabilityType,
    EmployeeAvailability,
    OverrideAction,
    Show,
    TimeOffStatus,
)
from scheduling.services.availability import effective_entries
from scheduling.services.engine import IncompleteAvailabilityError, SchedulingEngine
from scheduling.services.freshness import availability_freshness
from scheduling.services.overrides import (
    conflicts_on,
    is_in_conflict,
    keep_override,
    record_override,
    retire_override,
    square_time_off,
    square_view,
    supersedes_time_off,
    sync_owned_sources,
    use_square,
)
from scheduling.services.requirements import staffing_requirements_for
from scheduling.services.show_profiles import profile_for

STEP_LABELS = ("Show", "Guests", "Rules", "Who’s free", "Roster", "Review", "Publish")

ROLE_CLASSES = {
    "Server Manager": "role-server",
    "Server": "role-server",
    "Bartender": "role-bartender",
    "Busser": "role-busser",
    "50/50": "role-5050",
}

ROLE_FILTERS = (
    ("all", "All"),
    ("Server", "Server"),
    ("Bartender", "Bartender"),
    ("Busser", "Busser"),
    ("50/50", "50/50"),
)


def _steps(current: tuple[int, ...]) -> list[dict]:
    first = min(current)
    return [
        {
            "number": index,
            "label": label,
            "state": "now" if index in current else ("done" if index < first else ""),
        }
        for index, label in enumerate(STEP_LABELS, start=1)
    ]


def _parse_day(value: str | None, default: date) -> date:
    try:
        return datetime.strptime(value, "%Y-%m-%d").date() if value else default
    except ValueError:
        return default


def _shows_on(day: date) -> list[Show]:
    return list(
        Show.objects.filter(active=True, requires_service_staff=True, date=day).order_by(
            "start_time"
        )
    )


# ----------------------------------------------------------------- steps 1-3


@login_required
def roster_new(request):
    """Steps 1-3: the show, its guest count, and the rules it will be staffed by."""
    today = timezone.localdate()
    start = _parse_day(request.GET.get("from"), today)
    window_end = start + timedelta(days=13)
    shows = list(
        Show.objects.filter(
            active=True, requires_service_staff=True, date__range=(start, window_end)
        ).order_by("date", "start_time")
    )

    selected = None
    show_id = request.GET.get("show")
    if show_id:
        selected = get_object_or_404(Show, pk=show_id, active=True)
    elif shows:
        selected = shows[0]

    for show in shows:
        show.profile = profile_for(show)
        show.is_selected = selected is not None and show.pk == selected.pk

    context = {
        "steps": _steps((1, 2, 3)),
        "shows": shows,
        "selected": selected,
        "range_start": start,
        "range_end": window_end,
        "previous_from": start - timedelta(days=14),
        "next_from": start + timedelta(days=14),
        "freshness": availability_freshness(),
    }
    if selected is not None:
        context.update(_show_context(selected))
        context["guest_form"] = GuestCountForm(
            initial={"expected_guests": selected.planning_guest_count}
        )
    return render(request, "scheduling/roster_show.html", context)


def _show_context(show: Show) -> dict:
    profile = profile_for(show)
    requirements = []
    positions = 0
    if profile.staffed:
        reqs, over_capacity = staffing_requirements_for(show)
        for req in reqs:
            positions += req.confirmed_count + req.on_call_count
            requirements.append(
                {
                    "role": req.role_name,
                    "css": ROLE_CLASSES.get(req.role_name, ""),
                    "confirmed": req.confirmed_count,
                    "on_call": req.on_call_count,
                }
            )
    else:
        over_capacity = show.planning_guest_count > show.capacity
    day = show.date
    return {
        "profile": profile,
        "requirements": requirements,
        "positions": positions,
        "over_capacity": over_capacity,
        "same_day_shows": _shows_on(day),
        "seat_percent": min(100, round(show.planning_guest_count * 100 / MAX_THEATRE_CAPACITY)),
        "approved_time_off_count": _approved_time_off_count(day),
        "override_count": AvailabilityOverride.objects.filter(date=day, active=True).count(),
        "conflicts": conflicts_on(day),
    }


def _approved_time_off_count(day: date) -> int:
    from scheduling.models import EmployeeTimeOff

    return (
        EmployeeTimeOff.objects.filter(
            status=TimeOffStatus.APPROVED, start_date__lte=day, end_date__gte=day
        )
        .values("employee")
        .distinct()
        .count()
    )


@login_required
def roster_guests(request, show_id: int):
    """Step 2: change a show's guest count, keeping a note of what it was."""
    show = get_object_or_404(Show, pk=show_id, active=True)
    back = f"{reverse('roster_new')}?show={show.pk}&from={show.date:%Y-%m-%d}"
    if request.method != "POST":
        return redirect(back)
    form = GuestCountForm(request.POST)
    if not form.is_valid():
        messages.error(request, "Enter a guest count between 0 and 400.")
        return redirect(back)

    new_count = form.cleaned_data["expected_guests"]
    if new_count > show.capacity and not show.capacity_override_reason.strip():
        messages.error(
            request,
            f"{new_count} guests is over the {show.capacity}-seat capacity. Record a capacity "
            "override on the show first.",
        )
        return redirect(back)

    old = show.expected_guests
    if old != new_count:
        stamp = timezone.localtime().strftime("%Y-%m-%d %H:%M")
        who = request.user.get_username()
        was = "not set" if old is None else str(old)
        line = f"[{stamp}] Guest count changed from {was} to {new_count} by {who}."
        show.expected_guests = new_count
        show.notes = f"{show.notes.rstrip()}\n{line}".strip()
        show.save(update_fields=["expected_guests", "notes", "updated_at"])
        messages.success(
            request, f"Guest count set to {new_count}. The old count is in the show notes."
        )
    return redirect(back)


# ------------------------------------------------------------------- step 4


@dataclass
class StaffRow:
    employee: object
    roles: list[dict]
    square: str
    other_entries: str
    time_off: list[dict]
    override: AvailabilityOverride | None
    conflict: bool
    effective: str
    effective_tone: str
    effective_note: str = ""
    bucket: str = ""
    role_names: set = field(default_factory=set)


def _staff_row(employee, day: date, sync_sources: tuple[str, ...]) -> StaffRow:
    roles = [
        {"name": er.role.name, "css": ROLE_CLASSES.get(er.role.name, "")}
        for er in employee.employee_roles.all()
        if er.active and er.role.name in FLOOR_ROLE_NAMES
    ]
    view = square_view(employee, day)
    other = [
        row
        for row in EmployeeAvailability.objects.filter(employee=employee, date=day).exclude(
            source__in=sync_sources
        )
        if row.availability_type != AvailabilityType.UNKNOWN
    ]
    other_text = ", ".join(
        f"{r.start_time:%H:%M}–{r.end_time:%H:%M}"
        if r.availability_type == AvailabilityType.AVAILABLE_WINDOW and r.start_time
        else r.get_availability_type_display()
        for r in other
    )

    time_off = []
    approved = False
    pending = False
    for entry in square_time_off(employee, day):
        span = (
            f"{entry.start_date:%-d %b}"
            if entry.start_date == entry.end_date
            else f"{entry.start_date:%-d}–{entry.end_date:%-d %b}"
        )
        time_off.append(
            {
                "status": entry.get_status_display(),
                "span": span,
                "source": entry.get_source_display(),
                "tone": "danger" if entry.status == TimeOffStatus.APPROVED else "warning",
            }
        )
        approved = approved or entry.status == TimeOffStatus.APPROVED
        pending = pending or entry.status == TimeOffStatus.PENDING

    override = (
        AvailabilityOverride.objects.filter(employee=employee, date=day, active=True)
        .select_related("created_by")
        .first()
    )
    conflict = bool(override and is_in_conflict(override))

    if override and override.action == OverrideAction.REMOVE:
        effective, tone, bucket = "Unavailable (manual)", "manual", "manual"
    elif conflict:
        effective, tone, bucket = "Conflict", "danger", "conflict"
    elif approved and not supersedes_time_off(employee, day):
        effective, tone, bucket = "Time off", "danger", "off"
    else:
        entries, _ = effective_entries(employee, day)
        types = {e.availability_type for e in entries}
        manual = override is not None
        if AvailabilityType.AVAILABLE_ALL_DAY in types:
            effective, tone, bucket = "Available all day", "available", "available"
        elif AvailabilityType.AVAILABLE_WINDOW in types:
            windows = [
                e for e in entries if e.availability_type == AvailabilityType.AVAILABLE_WINDOW
            ]
            effective = "Available " + ", ".join(
                f"{w.start_time:%H:%M}–{w.end_time:%H:%M}" for w in windows if w.start_time
            )
            tone, bucket = "partial", "partial"
        elif AvailabilityType.UNAVAILABLE in types:
            effective, tone, bucket = "Unavailable", "muted", "unavailable"
        else:
            effective, tone, bucket = "Unknown", "muted", "unknown"
        if manual and bucket in {"available", "partial"}:
            effective += " (manual)"
            tone, bucket = "manual", "manual"

    note = ""
    if conflict:
        note = "Settle the conflict above before building the roster."
    elif bucket == "unknown":
        note = "Nothing on file, so the engine won't schedule them."
    elif pending and bucket != "off":
        note = "Time off pending — decide it in Square."
    elif override and override.action == OverrideAction.ADD and approved:
        note = "Your change overrules the approved time off."

    return StaffRow(
        employee=employee,
        roles=roles,
        square=view.summary,
        other_entries=other_text,
        time_off=time_off,
        override=override,
        conflict=conflict,
        effective=effective,
        effective_tone=tone,
        effective_note=note,
        bucket=bucket,
        role_names={r["name"] for r in roles},
    )


@login_required
def roster_availability(request, show_id: int):
    """Step 4: who can work the show, with manual changes and conflicts settled here."""
    show = get_object_or_404(Show, pk=show_id, active=True)
    day = show.date
    here = reverse("roster_availability", args=[show.pk])
    form = AvailabilityOverrideForm(initial={"employee": request.GET.get("employee")})

    if request.method == "POST":
        action = request.POST.get("action")
        if action == "add":
            form = AvailabilityOverrideForm(request.POST)
            if form.is_valid():
                data = form.cleaned_data
                try:
                    adding = data["change"] == OverrideAction.ADD
                    override = record_override(
                        data["employee"],
                        day,
                        data["change"],
                        data["reason"],
                        request.user,
                        data["start_time"] if adding else None,
                        data["end_time"] if adding else None,
                    )
                except ValidationError as exc:
                    form.add_error(None, exc)
                else:
                    messages.success(
                        request,
                        f"{override.employee.display_name}: {override.describe().lower()} "
                        f"for {day:%a %-d %b}. A Square refresh won't overwrite it.",
                    )
                    return redirect(here)
        elif action in {"retire", "keep", "use_square"}:
            override = get_object_or_404(
                AvailabilityOverride, pk=request.POST.get("override_id"), date=day, active=True
            )
            name = override.employee.display_name
            if action == "retire":
                retire_override(override, request.user, "Removed on the roster screen.")
                messages.success(request, f"Manual change for {name} removed.")
            elif action == "keep":
                keep_override(override, request.user)
                messages.success(request, f"Kept your change: {name} stays available.")
            else:
                use_square(override, request.user)
                messages.success(request, f"Using Square: {name} is off for this date.")
            return redirect(here)
        elif action == "generate":
            return _generate(request, show)

    sync_sources = sync_owned_sources()
    role_filter = request.GET.get("role", "all")
    staff = list(floor_staff().prefetch_related("employee_roles__role"))
    rows = [_staff_row(employee, day, sync_sources) for employee in staff]

    counts = {key: 0 for key in ("available", "partial", "off", "unavailable", "unknown")}
    manual_count = 0
    for row in rows:
        if row.override is not None:
            manual_count += 1
        if row.bucket in counts:
            counts[row.bucket] += 1
        elif row.bucket == "manual":
            # Counted by what the change does, so the tiles still add up to everyone.
            action = row.override.action if row.override else None
            counts["unavailable" if action == OverrideAction.REMOVE else "available"] += 1
        elif row.bucket == "conflict":
            counts["off"] += 1

    shown = [row for row in rows if role_filter == "all" or role_filter in row.role_names]
    log = (
        AvailabilityOverride.objects.filter(date=day)
        .select_related("employee", "created_by", "retired_by", "conflict_kept_by")
        .order_by("-created_at")[:20]
    )
    for entry in log:
        entry.in_conflict = is_in_conflict(entry)

    conflicts = conflicts_on(day)
    freshness = availability_freshness()
    context = {
        "steps": _steps((4,)),
        "show": show,
        "profile": profile_for(show),
        "same_day_shows": _shows_on(day),
        "rows": shown,
        "total_staff": len(rows),
        "tiles": [
            ("Available", counts["available"], "available"),
            ("Partial window", counts["partial"], "partial"),
            ("Time off", counts["off"], "danger"),
            ("Unavailable or unknown", counts["unavailable"] + counts["unknown"], "muted"),
            ("Manual changes", manual_count, "manual"),
        ],
        "role_filters": [
            {"key": key, "label": label, "on": key == role_filter} for key, label in ROLE_FILTERS
        ],
        "role_filter": role_filter,
        "form": form,
        "log": log,
        "conflicts": conflicts,
        "freshness": freshness,
        "unknown_count": counts["unknown"],
    }
    return render(request, "scheduling/roster_availability.html", context)


def _generate(request, show: Show):
    """Hand over to the engine for this show's date, then to the existing review page."""
    day = show.date
    here = reverse("roster_availability", args=[show.pk])
    conflicts = conflicts_on(day)
    if conflicts:
        names = ", ".join(c.employee.display_name for c in conflicts)
        messages.error(
            request, f"Settle the conflict first ({names}): keep your change or use Square’s."
        )
        return redirect(here)

    freshness = availability_freshness()
    use_stale = request.POST.get("use_stale") == "on"
    if freshness.stale and not use_stale:
        messages.error(
            request,
            "Square availability is more than 24 hours old. Refresh it, or tick the box to "
            "build from what is here.",
        )
        return redirect(here)

    allow_unknown = request.POST.get("allow_unknown") == "on"
    try:
        with transaction.atomic():
            run = SchedulingEngine().generate(
                day, day, created_by=request.user, allow_shortages=allow_unknown
            )
            note = (
                f"Built from the one-show roster screen for {show.title} on {day:%a %-d %b %Y}. "
                f"Square availability {freshness.age_text()}."
            )
            if freshness.stale:
                note += " Built on purpose from availability older than 24 hours."
            run.notes = f"{run.notes.rstrip()}\n{note}".strip()
            run.save(update_fields=["notes"])
    except IncompleteAvailabilityError:
        messages.error(
            request,
            "Some staff have no availability on file for this date. Tick “Build anyway” to "
            "continue — they stay off the roster.",
        )
        return redirect(here)
    except ValidationError as exc:
        messages.error(request, "; ".join(exc.messages))
        return redirect(here)

    messages.success(request, f"Roster #{run.pk} built for {day:%a %-d %b}. Review it below.")
    return redirect("schedule_detail", run_id=run.pk)
