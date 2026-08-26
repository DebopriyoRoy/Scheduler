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
from scheduling.services.allocator import GlobalAllocator, Slot, slot_priority
from scheduling.services.availability import AvailabilityProvider, LocalAvailabilityProvider
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


SHORTAGE_TYPES = {
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
        return schedule_run

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
        """Shift window for this role on this show, anchored to the show's own
        doors-open/wrap-up times rather than a fixed template clock time."""
        if template.code == "fifty-fifty":
            start = datetime.combine(show.date, FIFTY_FIFTY_START_TIME, tzinfo=LOCAL_TIMEZONE)
            end = datetime.combine(show.date, FIFTY_FIFTY_END_TIME, tzinfo=LOCAL_TIMEZONE)
            return start, end
        show_end_date = (
            show.date if show.end_time > show.start_time else show.date + timedelta(days=1)
        )
        doors = datetime.combine(show.date, show.start_time, tzinfo=LOCAL_TIMEZONE)
        wrap = datetime.combine(show_end_date, show.end_time, tzinfo=LOCAL_TIMEZONE)
        start_offset = ROLE_START_OFFSET_MINUTES.get(template.code, DEFAULT_START_OFFSET_MINUTES)
        end_offset = ROLE_END_OFFSET_MINUTES.get(template.code, DEFAULT_END_OFFSET_MINUTES)
        start = doors - timedelta(minutes=start_offset)
        end = wrap + timedelta(minutes=end_offset)
        return start, end

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
        spirit_only_ids: set[int] = set()
        carry_in_hours: dict[int, Decimal] = {}
        preferred_role_ids: dict[int, int] = {}
        for employee in Employee.objects.filter(active=True).select_related(
            "scheduling_preference"
        ):
            pref = getattr(employee, "scheduling_preference", None)
            if pref and pref.target_hours and pref.priority_enabled:
                targets[employee.id] = pref.target_hours
            if employee.spirit_only_employment or employee.employment_priority > 0:
                spirit_only_ids.add(employee.id)
            carry_in_hours[employee.id] = employee.opening_recent_hours
            if pref and pref.preferred_role_id:
                preferred_role_ids[employee.id] = pref.preferred_role_id

        allocator = GlobalAllocator(
            targets,
            spirit_only_ids,
            carry_in_hours=carry_in_hours,
            preferred_role_ids=preferred_role_ids,
        )

        # Stage A: build the full eligibility graph before committing anything.
        slots: list[Slot] = []
        excluded_by_slot: dict[tuple[int, int], dict[str, tuple[str, ...]]] = {}
        for show, template, _role_name in planned_slots:
            start, end = self._datetimes(show, template)
            candidates, excluded = self._eligible_candidates(schedule_run, show, template)
            slot = Slot(
                show=show,
                template=template,
                start=start,
                end=end,
                hours=Decimal(str(round((end - start).total_seconds() / 3600, 2))),
                is_on_call=template.assignment_type == AssignmentType.ON_CALL,
                candidates=[c.employee for c in candidates],
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
                result = self.eligibility.evaluate(
                    employee,
                    slot.template.role,
                    slot.show,
                    slot.template,
                    schedule_run,
                    slot.start,
                    slot.end,
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
                ordered = rotation.ordered_candidates({c.display_name for c in live_candidates})
                rotation_rank = {name: i for i, name in enumerate(ordered)}

            selected = max(
                live_candidates,
                key=lambda e: (
                    allocator.score(e, slot, remaining[e.id], max_shift_count),
                    -allocator.totals_for(e.id).shift_count,
                    -rotation_rank.get(e.display_name, 0),
                    e.display_name.casefold(),
                ),
            )
            reason = self._allocation_reason(allocator, selected, slot, len(live_candidates))
            if slot.template.role.name == "50/50":
                rotation.record_assignment(
                    selected.display_name, both_eligible=len(live_candidates) == 2
                )

            self._save_assignment(schedule_run, slot.show, slot.template, selected, reason)
            allocator.commit(selected, slot)
            self._snapshot_candidates(
                schedule_run, slot, live_candidates, selected, reason, allocator
            )

            for other in unfilled:
                if other.show.id == slot.show.id and selected in other.candidates:
                    other.candidates.remove(selected)

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
        if employee.id in allocator.spirit_only_ids:
            parts.append("Spirit-only priority applied (capped, decays at target)")
        return "; ".join(parts) + "."

    def _eligible_candidates(
        self,
        schedule_run: ScheduleRun,
        show: Show,
        template: ShiftTemplate,
    ) -> tuple[list[Candidate], dict[str, tuple[str, ...]]]:
        start, end = self._datetimes(show, template)
        candidates: list[Candidate] = []
        excluded: dict[str, tuple[str, ...]] = {}
        employees = Employee.objects.filter(active=True).prefetch_related("employee_roles__role")
        for employee in employees:
            result = self.eligibility.evaluate(
                employee,
                template.role,
                show,
                template,
                schedule_run,
                start,
                end,
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
        return candidates, excluded

    def _save_assignment(
        self,
        schedule_run: ScheduleRun,
        show: Show,
        template: ShiftTemplate,
        employee: Employee,
        reason: str,
    ) -> ScheduleAssignment:
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
        unknown_names = list(
            Employee.objects.filter(active=True)
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
