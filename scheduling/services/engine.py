from dataclasses import dataclass
from datetime import datetime, time, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import Count

from scheduling.models import (
    DEFAULT_EXPECTED_GUESTS,
    MINIMUM_CONFIRMED_SERVERS,
    MINIMUM_VIABLE_GUESTS,
    AssignmentType,
    AvailabilityType,
    Employee,
    EmployeeAvailability,
    OfficeAssignment,
    ScheduleAssignment,
    ScheduleRun,
    ScheduleRunStatus,
    SchedulingFairnessConfig,
    SchedulingFairnessSnapshot,
    SchedulingWarning,
    ShiftTemplate,
    Show,
    WarningSeverity,
    WarningType,
)
from scheduling.services.allocator import (
    MONTHLY_CONFIRMED_SHIFT_TARGET as MONTHLY_TARGET,
)
from scheduling.services.allocator import GlobalAllocator, Slot, slot_priority
from scheduling.services.availability import (
    ABSOLUTE_MIN_SHIFT_HOURS,
    MIN_FITTED_SHIFT_HOURS,
    AvailabilityProvider,
    LocalAvailabilityProvider,
)
from scheduling.services.eligibility import EXCLUDED_MANAGER_NAMES, EligibilityService
from scheduling.services.fairness import FairnessService
from scheduling.services.metrics import EmployeeMetrics, metrics_for_employee
from scheduling.services.requirements import (
    StaffingRequirement,
    staffing_requirements_for,
    templates_for_requirement,
)
from scheduling.services.rotations import FiftyFiftyRotation, generate_office_assignments

LOCAL_TIMEZONE = ZoneInfo("America/St_Johns")

# Shift windows are derived from each show's actual doors-open (Show.start_time)
# and wrap-up (Show.end_time) — pulled per-show from the live calendar/show-detail
# page — rather than a fixed clock time, since doors/act/dinner timing varies show
# to show. Values are minutes of setup before doors / wind-down after wrap.
ROLE_START_OFFSET_MINUTES = {
    "bartender": 60,
    "bartender-2": 60,
    "bartender-3": 60,
    "on-call-bartender": 0,
    "on-call-bartender-2": 0,
    "lead-server": 45,
    "server-2": 30,
    "server-3": 30,
    "server-4": 30,
    "server-5": 30,
    "server-6": 30,
    "server-7": 30,
    "on-call-server": 0,
    "on-call-server-2": 0,
    "on-call-server-3": 0,
    "busser": 30,
    "busser-2": 30,
}
ROLE_END_OFFSET_MINUTES = {
    "bartender": 15,
    "bartender-2": 15,
    "bartender-3": 15,
    "on-call-bartender": 0,
    "on-call-bartender-2": 0,
    "lead-server": 15,
    "server-2": 15,
    "server-3": 15,
    "server-4": 15,
    "server-5": 15,
    "server-6": 15,
    "server-7": 15,
    "on-call-server": 0,
    "on-call-server-2": 0,
    "on-call-server-3": 0,
    "busser": 30,
    "busser-2": 30,
}
DEFAULT_START_OFFSET_MINUTES = 30
DEFAULT_END_OFFSET_MINUTES = 15

# The 50/50 role (Yana/Kate: Server + Office Support) covers the core dinner-service
# window, not the full doors-to-wrap span — it is a fixed nightly slot by design,
# not anchored to each show's own timing the way the other floor roles are.
FIFTY_FIFTY_START_TIME = time(18, 0)
FIFTY_FIFTY_END_TIME = time(21, 30)


class IncompleteAvailabilityError(ValidationError):
    pass


class ApprovedScheduleError(ValidationError):
    pass


@dataclass(frozen=True)
class Candidate:
    employee: Employee
    metrics: EmployeeMetrics
    capability_level: int


# Management's own call times, by show type. These are clock times a person is told
# to arrive at, not offsets from doors: "Server 2 comes in at four" is the instruction
# that actually gets given, and deriving it from the curtain drifts whenever a show's
# recorded times move.
#
# Dwight's Wedding runs to its own timetable - doors 6:00pm, Act I 6:30pm, dinner
# 7:30pm - so its staff come in and leave at different clock times from an ordinary
# evening. It is matched on title rather than start time, because the calendar records
# some Dwight's dates against the curtain (18:30) and some against the doors (18:00);
# the title is the thing that reliably says which timetable applies.
STANDARD_EVENING_WINDOWS = {
    "server-manager": (time(14, 0), time(21, 0)),
    "lead-server": (time(15, 0), time(21, 0)),
    "server-2": (time(16, 0), time(21, 30)),
    "server-3": (time(17, 30), time(23, 0)),
    "on-call-server": (time(18, 15), time(23, 0)),
    "busser": (time(18, 45), time(23, 0)),
    "fifty-fifty": (time(17, 45), time(20, 30)),
    "bartender": (time(16, 0), time(23, 0)),
    "on-call-bartender": (time(17, 30), time(22, 30)),
}

DWIGHTS_WEDDING_WINDOWS = {
    "server-manager": (time(14, 0), time(21, 0)),
    "lead-server": (time(15, 0), time(21, 0)),
    "server-2": (time(15, 30), time(21, 30)),
    "server-3": (time(17, 0), time(22, 30)),
    "on-call-server": (time(19, 0), time(22, 30)),
    "busser": (time(19, 15), time(23, 0)),
    "fifty-fifty": (time(17, 30), time(20, 30)),
    "bartender": (time(16, 0), time(22, 30)),
    "on-call-bartender": (time(17, 0), time(22, 30)),
}

# A big night adds Server 4-6 and Bartender 2-3. They were given no call times of their
# own, so they take the last-in position's: Server 3 for extra servers, Bartender 1 for
# extra bartenders. Leaving them on the old derived offsets would put one crew on two
# different clocks for the same show.
WINDOW_ALIASES = {
    "server-4": "server-3",
    "server-5": "server-3",
    "server-6": "server-3",
    "on-call-server-2": "on-call-server",
    "on-call-server-3": "on-call-server",
    "bartender-2": "bartender",
    "bartender-3": "bartender",
    "on-call-bartender-2": "on-call-bartender",
}

# How far back the allocator looks when working out who has already had hours.
# Four weeks: long enough to even out a month's roster, short enough that someone
# who was away in the spring is not still owed shifts in the autumn.
RECENT_HOURS_WINDOW_DAYS = 28

# The evening timetable applies to a show that opens at half six.
STANDARD_EVENING_START = time(18, 30)

# The shows that run the house's regular timing - doors 6:30, dinner 7, curtain 8 -
# named so they are recognised however the calendar happens to record the clock.
# Matching on start time alone is fragile: Dwight's is proof that the same production
# gets entered against the doors on some dates and the curtain on others, and a show
# that drifts off 18:30 silently falls back to offsets derived from whatever times it
# does carry. Naming them means a typo in the calendar cannot quietly re-time a crew.
STANDARD_EVENING_TITLE_KEYWORDS = (
    "home-i-cide",
    "forever country",
    "shift happens",
)


def is_dwights_wedding(show: Show) -> bool:
    return "dwight" in (show.title or "").casefold()


def is_standard_evening_show(show: Show) -> bool:
    """A show on the house's regular doors-6:30 timetable."""
    title = (show.title or "").casefold()
    if any(keyword in title for keyword in STANDARD_EVENING_TITLE_KEYWORDS):
        return True
    return show.start_time == STANDARD_EVENING_START


def call_times_for(show: Show, template_code: str) -> tuple[time, time] | None:
    """The clock times management calls this position in for, or None to derive them."""
    code = WINDOW_ALIASES.get(template_code, template_code)
    if is_dwights_wedding(show):
        return DWIGHTS_WEDDING_WINDOWS.get(code)
    if is_standard_evening_show(show):
        return STANDARD_EVENING_WINDOWS.get(code)
    return None


def shift_window_for(show: Show, template: ShiftTemplate) -> tuple[datetime, datetime]:
    """Shift window for this role on this show.

    Management's fixed call times win where they exist. Anything else - a matinee, a
    private booking, an odd one-off - still derives its window from the show's own
    doors-open and wrap-up times, so an unusual date is staffed sensibly rather than
    forced onto an evening timetable that does not fit it.

    Module level so that filling a slot by hand starts from the same window the
    generator would have produced, instead of a second implementation drifting from it.
    """
    called = call_times_for(show, template.code)
    if called is None and template.code == "fifty-fifty":
        # A show on neither timetable - a matinee or a private booking - keeps the
        # long-standing default rather than deriving a raffle window from a curtain
        # time. Set here rather than returned early so it goes through the same window
        # construction as every other position.
        called = (FIFTY_FIFTY_START_TIME, FIFTY_FIFTY_END_TIME)
    if called is not None:
        start_time, end_time = called
        start = datetime.combine(show.date, start_time, tzinfo=LOCAL_TIMEZONE)
        end_date = show.date if end_time > start_time else show.date + timedelta(days=1)
        return start, datetime.combine(end_date, end_time, tzinfo=LOCAL_TIMEZONE)

    show_end_date = show.date if show.end_time > show.start_time else show.date + timedelta(days=1)
    doors = datetime.combine(show.date, show.start_time, tzinfo=LOCAL_TIMEZONE)
    wrap = datetime.combine(show_end_date, show.end_time, tzinfo=LOCAL_TIMEZONE)
    start_offset = ROLE_START_OFFSET_MINUTES.get(template.code, DEFAULT_START_OFFSET_MINUTES)
    end_offset = ROLE_END_OFFSET_MINUTES.get(template.code, DEFAULT_END_OFFSET_MINUTES)
    return doors - timedelta(minutes=start_offset), wrap + timedelta(minutes=end_offset)


def coverage_tier(slot, employee) -> int:
    """How well this person covers the published call window: lower is better.

    0 - the whole shift, which is what the timetable asks for and what is used
        whenever anyone at all can manage it.
    1 - a proper part-shift, three hours or more.
    2 - a short stretch, taken only to keep a position staffed rather than empty.

    Tiering rather than weighting matters: as a scoring term, a fully available person
    could be outbid by someone short on hours, and the published call times quietly
    shrank to whatever that person could do.
    """
    if slot.window_for(employee.id) == (slot.start, slot.end):
        return 0
    return 1 if float(slot.hours_for(employee.id)) >= MIN_FITTED_SHIFT_HOURS else 2


SHORTAGE_TYPES = {
    ("Server Manager", AssignmentType.CONFIRMED): WarningType.SERVER_MANAGER_SHORTAGE,
    ("Server", AssignmentType.CONFIRMED): WarningType.SERVER_SHORTAGE,
    ("Server", AssignmentType.ON_CALL): WarningType.ON_CALL_SERVER_SHORTAGE,
    ("Bartender", AssignmentType.CONFIRMED): WarningType.BARTENDER_SHORTAGE,
    ("Bartender", AssignmentType.ON_CALL): WarningType.ON_CALL_BARTENDER_SHORTAGE,
    ("Busser", AssignmentType.CONFIRMED): WarningType.BUSSER_SHORTAGE,
    ("50/50", AssignmentType.FIFTY_FIFTY): WarningType.FIFTY_FIFTY_SHORTAGE,
}


class SchedulingEngine:
    algorithm_version = "phase4-demand-ladder-v1"

    def __init__(
        self,
        availability_provider: AvailabilityProvider | None = None,
        fairness_config: SchedulingFairnessConfig | None = None,
    ):
        self.availability_provider = availability_provider or LocalAvailabilityProvider()
        self.eligibility = EligibilityService(self.availability_provider)
        self.fairness = FairnessService(config=fairness_config)

    @transaction.atomic
    def generate(
        self,
        start_date,
        end_date,
        *,
        created_by=None,
        allow_shortages: bool = False,
        schedule_run: ScheduleRun | None = None,
        calendar_sync_run=None,
        availability_sync_run=None,
    ) -> ScheduleRun:
        if end_date < start_date:
            raise ValidationError("End date must not precede the start date.")

        from scheduling.models import CalendarSyncRun, SquareAvailabilitySyncRun

        if calendar_sync_run is None:
            calendar_sync_run = CalendarSyncRun.objects.filter(status="SUCCESS").first()
        if availability_sync_run is None:
            availability_sync_run = SquareAvailabilitySyncRun.objects.filter(
                status__in=["SUCCESS", "PARTIAL"]
            ).first()

        shows = list(
            Show.objects.filter(
                active=True,
                requires_service_staff=True,
                date__range=(start_date, end_date),
            ).order_by("date", "start_time", "pk")
        )
        missing_count = self._missing_availability_count(shows)
        if missing_count and not allow_shortages:
            raise IncompleteAvailabilityError(
                f"{missing_count} employee/date availability entries are unknown. "
                "Choose Generate with shortages to continue without treating them as available."
            )

        if schedule_run is not None:
            if schedule_run.status in {
                ScheduleRunStatus.APPROVED,
                ScheduleRunStatus.SYNCED_TO_SQUARE,
            }:
                raise ApprovedScheduleError(
                    "Approved schedules cannot be regenerated. Create a new draft instead."
                )
            if schedule_run.status != ScheduleRunStatus.DRAFT:
                raise ValidationError("Only a draft schedule can be regenerated.")
            schedule_run.assignments.all().delete()
            schedule_run.warnings.all().delete()
            schedule_run.fairness_snapshots.all().delete()
            schedule_run.start_date = start_date
            schedule_run.end_date = end_date
            schedule_run.created_by = created_by or schedule_run.created_by
            schedule_run.calendar_sync_run = calendar_sync_run or schedule_run.calendar_sync_run
            schedule_run.availability_sync_run = (
                availability_sync_run or schedule_run.availability_sync_run
            )
            schedule_run.fairness_config = self.fairness.config
        else:
            schedule_run = ScheduleRun(
                start_date=start_date,
                end_date=end_date,
                created_by=created_by,
                calendar_sync_run=calendar_sync_run,
                availability_sync_run=availability_sync_run,
                fairness_config=self.fairness.config,
            )
        schedule_run.status = ScheduleRunStatus.GENERATING
        schedule_run.algorithm_version = self.algorithm_version
        schedule_run.full_clean()
        schedule_run.save()

        self._warn_about_overlapping_rosters(schedule_run)

        generate_office_assignments(start_date, end_date)
        rotation = FiftyFiftyRotation()
        planned_slots: list[tuple[Show, ShiftTemplate, str]] = []
        for show in shows:
            self._create_input_warnings(schedule_run, show)

            # Workshops (e.g. the Ugly Stick Workshop) are a class, not a dinner-theatre
            # show, and never receive floor/bar staffing.
            is_workshop = "workshop" in show.title.lower()
            if is_workshop:
                self._warning(
                    schedule_run,
                    show,
                    WarningType.EVENT_STAFFING_REVIEW_REQUIRED,
                    WarningSeverity.INFO,
                    "WORKSHOP_NOT_STAFFED: Workshop/class event does not receive "
                    "dinner-theatre staffing.",
                )
                continue

            # Section 10: Critical Offsite Rule
            is_offsite = "offsite" in show.title.lower() or "offsite" in show.venue.lower()
            if is_offsite:
                self._warning(
                    schedule_run,
                    show,
                    WarningType.EVENT_STAFFING_REVIEW_REQUIRED,
                    WarningSeverity.WARNING,
                    "OFFSITE_STAFFING_REVIEW_REQUIRED: Offsite event requires "
                    "manual management staffing review.",
                )
                continue

            # A show below the viability threshold does not run: management either
            # cancels it or moves those guests onto another date. Staffing it would
            # book crew for a night that will not happen, so the engine flags the
            # decision and leaves the show unstaffed.
            if show.planning_guest_count < MINIMUM_VIABLE_GUESTS:
                self._warning(
                    schedule_run,
                    show,
                    WarningType.EVENT_STAFFING_REVIEW_REQUIRED,
                    WarningSeverity.WARNING,
                    f"BELOW_VIABILITY_THRESHOLD: {show.planning_guest_count} guests is under "
                    f"the {MINIMUM_VIABLE_GUESTS}-guest minimum. Cancel the show or move "
                    "these guests to another date; no staff were scheduled.",
                )
                continue

            is_private = "private" in show.title.lower() and not is_offsite
            if is_private:
                self._warning(
                    schedule_run,
                    show,
                    WarningType.PRIVATE_EVENT_STAFFING_REVIEW_REQUIRED,
                    WarningSeverity.INFO,
                    "PRIVATE_EVENT_STAFFED: Private theatre event scheduled using standard "
                    "theatre staffing profile.",
                )

            requirements, outside_rules = staffing_requirements_for(show)
            if outside_rules:
                self._warning(
                    schedule_run,
                    show,
                    WarningType.HIGH_GUEST_COUNT_REVIEW,
                    WarningSeverity.WARNING,
                    f"{show.planning_guest_count} guests are outside approved staffing rules; "
                    "the highest configured staffing level was used and management review "
                    "is required.",
                )
            if not requirements:
                self._warning(
                    schedule_run,
                    show,
                    WarningType.ROLE_CONFIGURATION_ERROR,
                    WarningSeverity.ERROR,
                    "No applicable staffing rules are configured for this show.",
                )
                continue
            ordered = sorted(requirements, key=self._requirement_order)
            for requirement in ordered:
                templates = templates_for_requirement(requirement)
                expected = requirement.confirmed_count + requirement.on_call_count
                if len(templates) < expected:
                    self._warning(
                        schedule_run,
                        show,
                        WarningType.ROLE_CONFIGURATION_ERROR,
                        WarningSeverity.ERROR,
                        f"{requirement.role_name} needs {expected} position templates but only "
                        f"{len(templates)} are active.",
                    )
                for shift_template in templates:
                    planned_slots.append((show, shift_template, requirement.role_name))

        self._allocate_globally(schedule_run, planned_slots, rotation)

        self._enforce_minimum_server_floor(schedule_run, planned_slots)

        self._evaluate_fairness_alerts(schedule_run)

        incomplete = Employee.objects.filter(active=True, fairness_history_complete=False).exists()
        if incomplete:
            self._warning(
                schedule_run,
                None,
                WarningType.INSUFFICIENT_FAIRNESS_HISTORY,
                WarningSeverity.INFO,
                "At least one employee has no confirmed opening history; zero opening hours and "
                "shifts were used for those employees.",
            )
        needs_review = (
            schedule_run.warnings.filter(
                severity=WarningSeverity.ERROR,
                resolved=False,
            ).exists()
            or schedule_run.warnings.filter(
                warning_type=WarningType.HIGH_GUEST_COUNT_REVIEW,
                resolved=False,
            ).exists()
        )
        schedule_run.status = (
            ScheduleRunStatus.NEEDS_REVIEW if needs_review else ScheduleRunStatus.GENERATED
        )
        schedule_run.save(update_fields=["status"])
        self._supersede_earlier_runs(schedule_run)
        return schedule_run

    @staticmethod
    def _supersede_earlier_runs(schedule_run: ScheduleRun) -> int:
        """Retire older runs covering exactly this period.

        Regenerating a period used to leave the previous attempt sitting alongside the
        new one, both saying "Needs review", with nothing to tell you which was current
        beyond the run number. SUPERSEDED_SOURCE_DATA already existed for this and was
        read by the schedules page, but nothing ever set it.

        Only an identical start/end range is retired. Superseding anything that merely
        overlaps would let a single-day regeneration quietly retire the month around it.

        A run that has been sent to Square is never touched: the shifts are live there,
        so the record that explains them has to stay current. Approved runs are retired
        like any other - approval is a local decision, and the newer roster replaces it.
        """
        stale = (
            ScheduleRun.objects.filter(
                start_date=schedule_run.start_date,
                end_date=schedule_run.end_date,
            )
            .exclude(pk=schedule_run.pk)
            .exclude(
                status__in=(
                    ScheduleRunStatus.SYNCED_TO_SQUARE,
                    ScheduleRunStatus.SUPERSEDED_SOURCE_DATA,
                )
            )
        )
        return stale.update(status=ScheduleRunStatus.SUPERSEDED_SOURCE_DATA)

    def _enforce_minimum_server_floor(
        self,
        schedule_run: ScheduleRun,
        planned_slots: list[tuple[Show, ShiftTemplate, str]],
    ) -> None:
        """Every show that actually runs carries at least MINIMUM_CONFIRMED_SERVERS.

        The room never opens below MINIMUM_VIABLE_GUESTS, and one server per 25 guests
        makes three confirmed servers the floor at that count. Landing under it is an
        operational failure rather than a fairness nicety, so it escalates the whole run
        to management review instead of passing as a routine per-slot shortage.
        """
        staffed_shows = {show.id: show for show, _, _ in planned_slots}
        counts = (
            ScheduleAssignment.objects.filter(
                schedule_run=schedule_run,
                show_id__in=staffed_shows,
                role__name="Server",
                assignment_type=AssignmentType.CONFIRMED,
            )
            .values("show_id")
            .annotate(total=Count("id"))
        )
        filled = {row["show_id"]: row["total"] for row in counts}
        for show_id, show in staffed_shows.items():
            confirmed = filled.get(show_id, 0)
            if confirmed >= MINIMUM_CONFIRMED_SERVERS:
                continue
            self._warning(
                schedule_run,
                show,
                WarningType.SERVER_SHORTAGE,
                WarningSeverity.ERROR,
                f"BELOW_SERVER_FLOOR: {confirmed} confirmed server(s) scheduled but "
                f"{MINIMUM_CONFIRMED_SERVERS} is the operating minimum for any show that "
                "runs. Extend availability, call in a spare, or move these guests to "
                "another date.",
            )

    def _missing_availability_count(self, shows: list[Show]) -> int:
        dates = {show.date for show in shows}
        employees = Employee.objects.filter(active=True, excluded_from_automatic_scheduling=False)
        employees = [
            employee
            for employee in employees
            if employee.display_name.strip().casefold() not in EXCLUDED_MANAGER_NAMES
        ]
        known = EmployeeAvailability.objects.filter(
            employee__in=employees,
            date__in=dates,
        ).exclude(availability_type=AvailabilityType.UNKNOWN)
        return len(employees) * len(dates) - known.count()

    @staticmethod
    def _requirement_order(requirement: StaffingRequirement) -> int:
        return {"50/50": 0, "Bartender": 1, "Server": 2, "Busser": 3}.get(
            requirement.role_name,
            99,
        )

    def _datetimes(self, show: Show, template: ShiftTemplate) -> tuple[datetime, datetime]:
        return shift_window_for(show, template)

    def _allocate_globally(
        self,
        schedule_run: ScheduleRun,
        planned_slots: list[tuple[Show, ShiftTemplate, str]],
        rotation: FiftyFiftyRotation,
    ) -> None:
        """Fill every slot in the run using scarcity-ordered, deficit-driven selection.

        See scheduling.services.allocator for why this replaces per-show greedy
        assignment.
        """
        targets: dict[int, Decimal] = {}
        carry_in_hours: dict[int, Decimal] = {}
        recent = self._recent_paid_hours(schedule_run)
        recent_positions, recent_shifts = self._recent_position_history(schedule_run)
        preferred_role_ids: dict[int, int] = {}
        for employee in Employee.objects.filter(active=True).select_related(
            "scheduling_preference"
        ):
            pref = getattr(employee, "scheduling_preference", None)
            if pref and pref.target_hours and pref.priority_enabled:
                targets[employee.id] = pref.target_hours
            carry_in_hours[employee.id] = employee.opening_recent_hours + recent.get(
                employee.id, Decimal("0.00")
            )
            if pref and pref.preferred_role_id:
                preferred_role_ids[employee.id] = pref.preferred_role_id

        allocator = GlobalAllocator(
            targets,
            carry_in_hours=carry_in_hours,
            preferred_role_ids=preferred_role_ids,
            recent_position_counts=recent_positions,
            recent_shift_counts=recent_shifts,
        )

        # Stage A: build the full eligibility graph before committing anything.
        slots: list[Slot] = []
        excluded_by_slot: dict[tuple[int, int], dict[str, tuple[str, ...]]] = {}
        for show, template, _role_name in planned_slots:
            start, end = self._datetimes(show, template)
            candidates, excluded, fitted = self._eligible_candidates(
                schedule_run, show, template
            )
            slot = Slot(
                show=show,
                template=template,
                start=start,
                end=end,
                hours=Decimal(str(round((end - start).total_seconds() / 3600, 2))),
                is_on_call=template.assignment_type == AssignmentType.ON_CALL,
                candidates=[c.employee for c in candidates],
                fitted=fitted,
            )
            slots.append(slot)
            excluded_by_slot[slot.key] = excluded

        # Stage B: settle the most constrained slot first, each time re-validating
        # eligibility against assignments already committed in this run.
        unfilled = list(slots)
        while unfilled:
            unfilled.sort(key=slot_priority)
            slot = unfilled.pop(0)

            live_candidates = []
            for employee in slot.candidates:
                emp_start, emp_end = slot.window_for(employee.id)
                result = self.eligibility.evaluate(
                    employee,
                    slot.template.role,
                    slot.show,
                    slot.template,
                    schedule_run,
                    emp_start,
                    emp_end,
                )
                if result.eligible:
                    live_candidates.append(employee)
                else:
                    excluded_by_slot[slot.key][employee.display_name] = result.reasons

            if not live_candidates:
                self._shortage(
                    schedule_run, slot.show, slot.template, excluded_by_slot[slot.key]
                )
                continue

            if slot.template.role.name == "50/50":
                live_candidates, spared = self._reserve_for_the_floor(
                    slot, unfilled, live_candidates
                )
                for employee in spared:
                    excluded_by_slot[slot.key][employee.display_name] = (
                        "Needed on the floor or the bar for this show; the 50/50 seat "
                        "cannot take the last person a required position has.",
                    )
                if not live_candidates:
                    self._shortage(
                        schedule_run, slot.show, slot.template, excluded_by_slot[slot.key]
                    )
                    continue

            # Management's call times are the rule, not a suggestion. Anyone who can
            # work the whole window is preferred outright over anyone who can only work
            # part of it - a partial shift is what you fall back on when the slot would
            # otherwise go to nobody, not something to hand out while a fully available
            # person is standing there. Weighting this instead of gating it let a
            # starved candidate with narrow hours outbid full cover and quietly shrink
            # the published call times, which is exactly what must not happen.
            tiers = {e.id: coverage_tier(slot, e) for e in live_candidates}
            best_tier = min(tiers.values())
            pool = [e for e in live_candidates if tiers[e.id] == best_tier]

            # Everyone available is owed real shifts, not only the few whose hours
            # happen to fit the widest windows. When every full-cover candidate has
            # already had a fair month and somebody who can work most of the shift has
            # not, let them into the running. It is self-limiting: early in a month the
            # full-cover people are short too, so they win and the call times stand.
            if best_tier == 0 and not slot.is_on_call:
                fewest_full_cover = min(
                    allocator.confirmed_shifts_so_far(e.id) for e in pool
                )
                pool += [
                    e
                    for e in live_candidates
                    if tiers[e.id] == 1
                    and allocator.confirmed_shifts_so_far(e.id) < MONTHLY_TARGET
                    and allocator.confirmed_shifts_so_far(e.id) < fewest_full_cover
                ]
            live_candidates = pool

            remaining = {
                employee.id: sum(1 for other in unfilled if employee in other.candidates)
                for employee in live_candidates
            }
            max_shift_count = max(
                (allocator.totals_for(e.id).shift_count for e in live_candidates),
                default=0,
            )
            # The 50/50 rotation is a tie-break, not the driver: a blind alternation
            # hands a slot to whoever is "next" even when the other candidate can
            # only work that one date. Deficit and scarcity lead; rotation order
            # settles genuine ties, which is the fair-resume behaviour the spec asks
            # for rather than restarting the sequence blindly.
            rotation_rank = {}
            if slot.template.role.name == "50/50":
                ordered = rotation.ordered_candidates({c.first_name for c in live_candidates})
                rotation_rank = {name: i for i, name in enumerate(ordered)}

            selected = max(
                live_candidates,
                key=lambda e: (
                    allocator.score(e, slot, remaining[e.id], max_shift_count),
                    -allocator.totals_for(e.id).shift_count,
                    -rotation_rank.get(e.first_name, 0),
                    e.display_name.casefold(),
                ),
            )
            reason = self._allocation_reason(allocator, selected, slot, len(live_candidates))
            if slot.template.role.name == "50/50":
                rotation.record_assignment(
                    selected.first_name, both_eligible=len(live_candidates) == 2
                )

            chosen_start, chosen_end = slot.window_for(selected.id)
            self._save_assignment(
                schedule_run,
                slot.show,
                slot.template,
                selected,
                reason,
                start=chosen_start,
                end=chosen_end,
            )
            if (chosen_start, chosen_end) != (slot.start, slot.end):
                self._partial_coverage_warning(
                    schedule_run, slot, selected, chosen_start, chosen_end
                )
            allocator.commit(selected, slot)
            self._snapshot_candidates(
                schedule_run, slot, live_candidates, selected, reason, allocator
            )

            for other in unfilled:
                if other.show.id == slot.show.id and selected in other.candidates:
                    other.candidates.remove(selected)

    def _reserve_for_the_floor(
        self,
        slot,
        unfilled: list,
        live_candidates: list[Employee],
    ) -> tuple[list[Employee], list[Employee]]:
        """Keep the raffle from taking someone a required position cannot spare.

        Both 50/50 sellers also serve, and 50/50 is settled first because it is the
        most constrained slot on the board - only two people hold the role. That
        combination emptied the floor: across one fortnight Yana took five 50/50 seats
        and served on none, while three server and five on-call server positions went
        unfilled, because the raffle had already booked her by the time they were
        settled.

        The rule management actually work to is that the floor and the bar come first
        and the raffle takes whoever is genuinely spare. So a candidate is withheld
        when losing them would leave this show's required positions with fewer people
        than seats - Hall's condition, counted across the pool the remaining essential
        slots share rather than slot by slot, because those slots draw on the same
        people.

        Ordering alone cannot express this. Settling 50/50 last would starve the
        rotation instead: both sellers would be taken as servers on every show that
        had a seat free, and nobody would sell tickets.
        """
        from scheduling.services.allocator import role_tier

        essential = [
            other
            for other in unfilled
            if other.show.id == slot.show.id
            and role_tier(other.template.role.name) == 0
            and not other.is_on_call
        ]
        if not essential:
            return live_candidates, []

        pool = {employee.id for other in essential for employee in other.candidates}
        kept, spared = [], []
        for employee in live_candidates:
            if employee.id in pool and len(pool - {employee.id}) < len(essential):
                spared.append(employee)
            else:
                kept.append(employee)
        return kept, spared

    def _snapshot_candidates(
        self,
        schedule_run: ScheduleRun,
        slot: Slot,
        candidates: list[Employee],
        selected: Employee,
        reason: str,
        allocator: GlobalAllocator,
    ) -> None:
        """Persist why each eligible candidate ranked where they did.

        Selection is driven by the allocator, but the full fairness component
        breakdown is still recorded so management can audit any decision.
        """
        capability_levels = {}
        for employee in candidates:
            employee_role = next(
                (
                    item
                    for item in employee.employee_roles.all()
                    if item.active and item.role_id == slot.template.role_id
                ),
                None,
            )
            capability_levels[employee.id] = (
                employee_role.capability_level if employee_role else 3
            )

        fairness_map = self.fairness.evaluate_candidates(
            candidates,
            slot.template.role,
            slot.show,
            slot.template,
            schedule_run,
            capability_levels,
            eligibility_service=self.eligibility,
        )
        for employee in candidates:
            cfm = fairness_map[employee.id]
            is_chosen = employee.id == selected.id
            totals = allocator.totals_for(employee.id)
            SchedulingFairnessSnapshot.objects.create(
                schedule_run=schedule_run,
                employee=employee,
                role=slot.template.role,
                eligible_opportunities=cfm.eligible_opportunities,
                confirmed_opportunities=cfm.confirmed_opportunities,
                opportunity_rate=Decimal(str(round(cfm.opportunity_rate, 2))),
                recent_actual_hours=Decimal(str(round(cfm.recent_actual_hours, 2))),
                recent_confirmed_shifts=cfm.recent_confirmed_shifts,
                recent_on_call_assignments=cfm.recent_on_call_assignments,
                recent_on_call_hours=Decimal(str(round(cfm.recent_on_call_hours, 2))),
                recent_weekend_shifts=cfm.recent_weekend_shifts,
                consecutive_nights=cfm.consecutive_nights,
                role_opportunities=cfm.role_opportunities,
                target_hours=Decimal(str(round(cfm.target_hours, 2))) if cfm.target_hours else None,
                projected_hours=Decimal(str(round(float(totals.paid_hours), 2))),
                reliability_score=Decimal(str(round(cfm.reliability_score, 2))),
                performance_score=Decimal(str(round(cfm.performance_score, 2))),
                role_fit_score=Decimal(str(round(cfm.role_fit_score, 2))),
                target_hours_adjustment=Decimal(str(round(cfm.target_hours_adjustment, 2))),
                confirmed_fair_score=Decimal(str(round(cfm.confirmed_fair_score, 3))),
                on_call_fair_score=Decimal(str(round(cfm.on_call_fair_score, 3))),
                selected=is_chosen,
                selection_reason=reason if is_chosen else "",
            )

    @staticmethod
    def _allocation_reason(
        allocator: GlobalAllocator,
        employee: Employee,
        slot: Slot,
        pool_size: int,
    ) -> str:
        totals = allocator.totals_for(employee.id)
        if slot.is_on_call:
            deficit = allocator.on_call_deficit_ratio(employee.id)
            basis = f"on-call hours so far={totals.on_call_hours}"
        else:
            deficit = allocator.hours_deficit_ratio(employee.id)
            basis = f"confirmed paid hours so far={totals.paid_hours}"
        target = allocator.targets.get(employee.id)
        parts = [
            "All hard constraints passed",
            basis,
            f"target={target if target is not None else 'unset'}",
            f"deficit={deficit:+.2f}",
            f"shifts so far={totals.shift_count}",
            f"ranked #1 of {pool_size} eligible by global deficit allocation",
        ]
        return "; ".join(parts) + "."

    def _recent_paid_hours(self, schedule_run: ScheduleRun) -> dict[int, Decimal]:
        """Confirmed hours each employee already has in the weeks before this run.

        Without this the allocator had no memory: carry-in came from
        Employee.opening_recent_hours, a field nothing ever writes, so every run started
        the whole roster at zero and the same tie-break order won every time. That is
        why a fresh single-date run kept producing the same names in the same slots.
        Superseded runs are left out - they were replaced, so the hours never existed -
        as is the run being built, whose own hours the allocator tracks live.
        """
        window_start = schedule_run.start_date - timedelta(days=RECENT_HOURS_WINDOW_DAYS)
        rows = (
            ScheduleAssignment.objects.filter(
                show__date__gte=window_start,
                show__date__lt=schedule_run.start_date,
                assignment_type=AssignmentType.CONFIRMED,
                employee__isnull=False,
            )
            .exclude(schedule_run=schedule_run)
            .exclude(schedule_run__status=ScheduleRunStatus.SUPERSEDED_SOURCE_DATA)
            .values_list("employee_id", "scheduled_paid_hours")
        )
        totals: dict[int, Decimal] = {}
        for employee_id, hours in rows:
            totals[employee_id] = totals.get(employee_id, Decimal("0.00")) + hours
        return totals

    def _recent_position_history(
        self, schedule_run: ScheduleRun
    ) -> tuple[dict[tuple[int, int], int], dict[int, int]]:
        """Who has held which position lately, and how many shifts they have had.

        The first drives position rotation - so the person who starts at three is not
        the same person every night - and the second drives the monthly floor, so
        everyone who is available gets real shifts rather than only the few whose
        availability happens to cover the widest windows.
        """
        window_start = schedule_run.start_date - timedelta(days=RECENT_HOURS_WINDOW_DAYS)
        rows = (
            ScheduleAssignment.objects.filter(
                show__date__gte=window_start,
                show__date__lt=schedule_run.start_date,
                employee__isnull=False,
            )
            .exclude(schedule_run=schedule_run)
            .exclude(schedule_run__status=ScheduleRunStatus.SUPERSEDED_SOURCE_DATA)
            .values_list("employee_id", "shift_template_id", "assignment_type")
        )
        positions: dict[tuple[int, int], int] = {}
        shifts: dict[int, int] = {}
        for employee_id, template_id, assignment_type in rows:
            key = (employee_id, template_id)
            positions[key] = positions.get(key, 0) + 1
            # The 50/50 is a real night's work, not standby, so it counts towards a
            # fair month. Counting only CONFIRMED made whoever works the 50/50 look
            # starved and pushed them up the order for floor shifts they did not need.
            if assignment_type != AssignmentType.ON_CALL:
                shifts[employee_id] = shifts.get(employee_id, 0) + 1
        return positions, shifts

    def _fit_to_availability(
        self,
        employee: Employee,
        show: Show,
        template: ShiftTemplate,
        start: datetime,
        end: datetime,
    ) -> tuple[datetime, datetime]:
        """Trim a call window to the part this employee is free for.

        Returns the window unchanged when there is nothing to trim to - no availability
        on file, or a provider that cannot fit - so the ordinary eligibility checks give
        their usual verdict and an unknown stays a hard no.
        """
        fit = getattr(self.availability_provider, "fit", None)
        if fit is None:
            return start, end
        window = fit(
            employee,
            show.date,
            start.time(),
            end.time(),
            minimum_hours=ABSOLUTE_MIN_SHIFT_HOURS,
        )
        if window is None or window.covers_full_shift:
            return start, end
        fitted_start = datetime.combine(show.date, window.start_time, tzinfo=LOCAL_TIMEZONE)
        wraps = window.end_time <= window.start_time
        end_date = show.date + timedelta(days=1) if wraps else show.date
        return fitted_start, datetime.combine(end_date, window.end_time, tzinfo=LOCAL_TIMEZONE)

    def _eligible_candidates(
        self,
        schedule_run: ScheduleRun,
        show: Show,
        template: ShiftTemplate,
    ) -> tuple[list[Candidate], dict[str, tuple[str, ...]], dict[int, tuple[datetime, datetime]]]:
        start, end = self._datetimes(show, template)
        candidates: list[Candidate] = []
        excluded: dict[str, tuple[str, ...]] = {}
        fitted: dict[int, tuple[datetime, datetime]] = {}
        employees = Employee.objects.filter(active=True).prefetch_related("employee_roles__role")
        for employee in employees:
            # Judge each person against the part of the shift they can actually work,
            # not the whole call window. Someone free 16:00-20:30 is not unavailable for
            # a shift ending at 23:00; they are available for the first three hours.
            emp_start, emp_end = self._fit_to_availability(employee, show, template, start, end)
            if (emp_start, emp_end) != (start, end):
                fitted[employee.id] = (emp_start, emp_end)
            result = self.eligibility.evaluate(
                employee,
                template.role,
                show,
                template,
                schedule_run,
                emp_start,
                emp_end,
            )
            if not result.eligible:
                excluded[employee.display_name] = result.reasons
                continue
            employee_role = next(
                item
                for item in employee.employee_roles.all()
                if item.active and item.role_id == template.role_id
            )
            candidates.append(
                Candidate(
                    employee=employee,
                    metrics=metrics_for_employee(employee, schedule_run),
                    capability_level=employee_role.capability_level,
                )
            )
        return candidates, excluded, fitted

    def _save_assignment(
        self,
        schedule_run: ScheduleRun,
        show: Show,
        template: ShiftTemplate,
        employee: Employee,
        reason: str,
        start: datetime | None = None,
        end: datetime | None = None,
    ) -> ScheduleAssignment:
        if start is None or end is None:
            start, end = self._datetimes(show, template)
        duration_hours = Decimal(str(round((end - start).total_seconds() / 3600, 2)))
        is_on_call = template.assignment_type == AssignmentType.ON_CALL
        assignment = ScheduleAssignment(
            schedule_run=schedule_run,
            show=show,
            employee=employee,
            role=template.role,
            assignment_type=template.assignment_type,
            shift_template=template,
            start_datetime=start,
            end_datetime=end,
            scheduled_paid_hours=Decimal("0.00") if is_on_call else duration_hours,
            on_call_hours=duration_hours if is_on_call else Decimal("0.00"),
            selection_reason=reason,
        )
        assignment.full_clean()
        assignment.save()
        return assignment

    def _partial_coverage_warning(
        self,
        schedule_run: ScheduleRun,
        slot,
        employee: Employee,
        start: datetime,
        end: datetime,
    ) -> None:
        """Say plainly which hours of the shift nobody is on.

        Filling a slot with somebody who can work part of it is better than leaving it
        empty, but it is not the same as covering it. Without this the tail of the
        evening would quietly go unstaffed with nothing on the schedule to show for it.
        """
        gaps = []
        if start > slot.start:
            gaps.append(f"{slot.start:%H:%M}-{start:%H:%M}")
        if end < slot.end:
            gaps.append(f"{end:%H:%M}-{slot.end:%H:%M}")
        self._warning(
            schedule_run,
            slot.show,
            WarningType.PARTIAL_SHIFT_COVERAGE,
            WarningSeverity.WARNING,
            f"{employee.display_name} covers {start:%H:%M}-"
            f"{end:%H:%M} of {slot.template.name} "
            f"({slot.start:%H:%M}-{slot.end:%H:%M}); "
            f"nobody is on for {' and '.join(gaps)}. Their availability does not reach "
            f"further - add a second person for the rest if the floor needs it.",
        )

    def _shortage(
        self,
        schedule_run: ScheduleRun,
        show: Show,
        template: ShiftTemplate,
        excluded: dict[str, tuple[str, ...]],
    ) -> None:
        warning_type = SHORTAGE_TYPES.get(
            (template.role.name, template.assignment_type),
            WarningType.ROLE_CONFIGURATION_ERROR,
        )
        sample = "; ".join(
            f"{name}: {', '.join(reasons)}" for name, reasons in list(sorted(excluded.items()))[:4]
        )
        self._warning(
            schedule_run,
            show,
            warning_type,
            WarningSeverity.ERROR,
            f"No eligible employee for {template.name}."
            + (f" Examples: {sample}" if sample else ""),
        )

    def _warn_about_overlapping_rosters(self, schedule_run: ScheduleRun) -> None:
        """Say plainly when these dates are already staffed by a live roster.

        Nobody can work two shifts at once, so anyone already rostered on an approved
        or synced run is refused here - correctly. But that refusal only ever appeared
        as one line inside each unfilled position, behind a "Why?" link, so a run
        generated over dates Square already holds looked like a broken engine: forty-six
        shortages, every server empty, and no explanation on the page.

        The overlap is a property of the whole run, so it is reported once, at the top,
        naming the run responsible and what to do about it.
        """
        live = (
            ScheduleRun.objects.filter(
                status__in=[ScheduleRunStatus.APPROVED, ScheduleRunStatus.SYNCED_TO_SQUARE],
                start_date__lte=schedule_run.end_date,
                end_date__gte=schedule_run.start_date,
            )
            .exclude(pk=schedule_run.pk)
            .order_by("start_date", "pk")
        )
        for other in live:
            staff = (
                ScheduleAssignment.objects.filter(schedule_run=other)
                .values("employee")
                .distinct()
                .count()
            )
            self._warning(
                schedule_run,
                None,
                WarningType.OVERLAPPING_ROSTER,
                WarningSeverity.WARNING,
                f"Schedule #{other.pk} ({other.get_status_display()}) already rosters "
                f"{other.start_date:%d %b} to {other.end_date:%d %b %Y}, which overlaps "
                f"these dates, and has {staff} member(s) of staff on it. Nobody can work "
                f"two shifts at once, so anyone already booked there cannot be placed "
                f"here and those positions will show as shortages. To re-plan these "
                f"dates, edit schedule #{other.pk} instead, or supersede it first.",
            )

    def _create_input_warnings(self, schedule_run: ScheduleRun, show: Show) -> None:
        if show.uses_default_guest_count:
            self._warning(
                schedule_run,
                show,
                WarningType.GUEST_COUNT_DEFAULTED,
                WarningSeverity.INFO,
                f"Expected guests were not supplied; the {DEFAULT_EXPECTED_GUESTS}-guest "
                f"planning buffer was used (shows run at {MINIMUM_VIABLE_GUESTS}-"
                f"{DEFAULT_EXPECTED_GUESTS} guests or are cancelled). Enter the real "
                "guest count to move this show up the staffing ladder.",
            )
        # Only people the engine could actually roster. Kitchen, office and cleaning
        # staff hold no scheduled role, so naming them as "availability unknown" was
        # noise about staff this application never schedules in the first place.
        unknown_names = list(
            Employee.objects.filter(active=True, employee_roles__active=True)
            .distinct()
            .exclude(
                availability_entries__date=show.date,
                availability_entries__availability_type__in=[
                    AvailabilityType.AVAILABLE_ALL_DAY,
                    AvailabilityType.AVAILABLE_WINDOW,
                    AvailabilityType.UNAVAILABLE,
                ],
            )
            .values_list("display_name", flat=True)
        )
        if unknown_names:
            self._warning(
                schedule_run,
                show,
                WarningType.UNKNOWN_AVAILABILITY,
                WarningSeverity.WARNING,
                f"Unknown availability for {len(unknown_names)} employee(s): "
                + ", ".join(sorted(unknown_names)),
            )
        templates = list(ShiftTemplate.objects.filter(active=True))
        for office in OfficeAssignment.objects.filter(date=show.date).select_related("employee"):
            overlapping = [
                template.name
                for template in templates
                if office.start_time < template.end_time and office.end_time > template.start_time
            ]
            if overlapping:
                self._warning(
                    schedule_run,
                    show,
                    WarningType.OFFICE_CONFLICT,
                    WarningSeverity.INFO,
                    f"{office.employee.display_name}'s office assignment overlaps: "
                    + ", ".join(overlapping)
                    + ". The employee remains ineligible only for those overlapping times.",
                )

    @staticmethod
    def _warning(schedule_run, show, warning_type, severity, message) -> SchedulingWarning:
        return SchedulingWarning.objects.create(
            schedule_run=schedule_run,
            show=show,
            warning_type=warning_type,
            severity=severity,
            message=message,
        )

    def _evaluate_fairness_alerts(self, schedule_run: ScheduleRun) -> None:
        snapshots = list(
            SchedulingFairnessSnapshot.objects.filter(
                schedule_run=schedule_run, selected=True
            ).select_related("employee")
        )
        if not snapshots:
            return

        opp_rates = [float(s.opportunity_rate) for s in snapshots]
        if opp_rates and (max(opp_rates) - min(opp_rates) > 0.40):
            self._warning(
                schedule_run,
                None,
                WarningType.OPPORTUNITY_IMBALANCE,
                WarningSeverity.INFO,
                f"OPPORTUNITY_IMBALANCE: Opportunity rates range from "
                f"{min(opp_rates):.0%} to {max(opp_rates):.0%}.",
            )

        on_calls = [s.recent_on_call_assignments for s in snapshots]
        if on_calls and (max(on_calls) - min(on_calls) > 3):
            self._warning(
                schedule_run,
                None,
                WarningType.ON_CALL_IMBALANCE,
                WarningSeverity.INFO,
                f"ON_CALL_IMBALANCE: On-call shift counts range from "
                f"{min(on_calls)} to {max(on_calls)}.",
            )

        weekends = [s.recent_weekend_shifts for s in snapshots]
        if weekends and (max(weekends) - min(weekends) > 4):
            self._warning(
                schedule_run,
                None,
                WarningType.WEEKEND_IMBALANCE,
                WarningSeverity.INFO,
                f"WEEKEND_IMBALANCE: Weekend shift counts range from "
                f"{min(weekends)} to {max(weekends)}.",
            )
