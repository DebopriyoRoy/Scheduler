from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

from django.core.exceptions import ValidationError
from django.db import transaction

from scheduling.models import (
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
    algorithm_version = "phase3-fairness-v1"

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
        for show in shows:
            self._create_input_warnings(schedule_run, show)

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
                    if requirement.role_name == "50/50":
                        self._assign_fifty_fifty(schedule_run, show, shift_template, rotation)
                    else:
                        self._assign_ranked(schedule_run, show, shift_template)

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
        start = datetime.combine(show.date, template.start_time, tzinfo=LOCAL_TIMEZONE)
        end_date = (
            show.date if template.end_time > template.start_time else show.date + timedelta(days=1)
        )
        end = datetime.combine(end_date, template.end_time, tzinfo=LOCAL_TIMEZONE)
        return start, end

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

    def _candidate_key(self, candidate: Candidate, template: ShiftTemplate, show: Show):
        metrics = candidate.metrics
        employee = candidate.employee
        consecutive_previous_night = ScheduleAssignment.objects.filter(
            schedule_run__status__in=[
                ScheduleRunStatus.GENERATING,
                ScheduleRunStatus.APPROVED,
                ScheduleRunStatus.SYNCED_TO_SQUARE,
            ],
            employee=employee,
            show__date=show.date - timedelta(days=1),
        ).exists()
        cross_trained_server = employee.employee_roles.filter(
            role__name="Server",
            active=True,
        ).exists()
        if template.assignment_type == AssignmentType.ON_CALL:
            return (
                metrics.on_call_assignment_count,
                metrics.confirmed_paid_hours,
                metrics.weekend_assignment_count,
                consecutive_previous_night,
                employee.display_name.casefold(),
            )
        bartender_protection = cross_trained_server if template.role.name == "Bartender" else False
        priority_adjusted_hours = metrics.confirmed_paid_hours - Decimal(
            employee.employment_priority * 4
        )
        lead_capability = -candidate.capability_level if template.code == "lead-server" else 0
        return (
            bartender_protection,
            priority_adjusted_hours,
            metrics.confirmed_shift_count,
            metrics.weekend_assignment_count,
            consecutive_previous_night,
            lead_capability,
            employee.display_name.casefold(),
        )

    def _assign_ranked(
        self,
        schedule_run: ScheduleRun,
        show: Show,
        template: ShiftTemplate,
    ) -> ScheduleAssignment | None:
        candidates, excluded = self._eligible_candidates(schedule_run, show, template)
        if not candidates:
            self._shortage(schedule_run, show, template, excluded)
            return None

        # STAGE 3: Scarce Bartender Protection
        if template.role.name == "Server" and template.code != "lead-server":
            unfilled_bartender = [
                t
                for t in ShiftTemplate.objects.filter(role__name="Bartender", active=True)
                if not ScheduleAssignment.objects.filter(
                    schedule_run=schedule_run, show=show, shift_template=t
                ).exists()
            ]
            if unfilled_bartender:
                avail_bar_capable = sum(
                    1
                    for c in candidates
                    if c.employee.employee_roles.filter(
                        role__name="Bartender", active=True
                    ).exists()
                    or c.capability_level >= 4
                )
                if avail_bar_capable <= len(unfilled_bartender):
                    filtered_candidates = []
                    for c in candidates:
                        is_bar_capable = (
                            c.employee.employee_roles.filter(
                                role__name="Bartender", active=True
                            ).exists()
                            or c.capability_level >= 4
                        )
                        if is_bar_capable and c.employee.employee_roles.filter(
                            role__name="Server", active=True
                        ).exists():
                            excluded[c.employee.display_name] = (
                                "Reserved for Bartender coverage.",
                            )
                        else:
                            filtered_candidates.append(c)
                    if filtered_candidates:
                        candidates = filtered_candidates

        if not candidates:
            self._shortage(schedule_run, show, template, excluded)
            return None

        # STAGE 4: Fairness Ranking & Evaluation
        capability_levels = {c.employee.id: c.capability_level for c in candidates}
        fairness_map = self.fairness.evaluate_candidates(
            [c.employee for c in candidates],
            template.role,
            show,
            template,
            schedule_run,
            capability_levels,
            eligibility_service=self.eligibility,
        )

        def candidate_sort_key(c: Candidate):
            fm = fairness_map[c.employee.id]
            is_cross_trained = c.employee.employee_roles.filter(
                role__name="Server", active=True
            ).exists()
            if template.role.name == "Bartender":
                bartender_protection = 1 if is_cross_trained else 0
            else:
                bartender_protection = 0

            if template.assignment_type == AssignmentType.ON_CALL:
                return (
                    bartender_protection,
                    -fm.on_call_fair_score,
                    fm.recent_on_call_assignments,
                    fm.recent_on_call_hours,
                    fm.opportunity_rate,
                    c.employee.display_name.casefold(),
                )

            lead_cap = -c.capability_level if template.code == "lead-server" else 0
            return (
                bartender_protection,
                lead_cap,
                -fm.confirmed_fair_score,
                fm.opportunity_rate,
                fm.recent_actual_hours,
                fm.recent_on_call_assignments,
                c.employee.display_name.casefold(),
            )

        selected = min(candidates, key=candidate_sort_key)
        fm = fairness_map[selected.employee.id]

        if template.assignment_type == AssignmentType.ON_CALL:
            reason = (
                f"Selected for On-Call {template.name}: OnCallFairScore "
                f"{fm.on_call_fair_score:.3f} (OnCallCntFair: {fm.on_call_count_fairness:.3f}, "
                f"OppFair: {fm.opportunity_fairness:.3f}). Ranked #1 among "
                f"{len(candidates)} eligible candidate(s)."
            )
        else:
            reason = (
                f"Selected as {template.name}: FairScore {fm.confirmed_fair_score:.3f} "
                f"(OppFair: {fm.opportunity_fairness:.3f}, HoursFair: {fm.hours_fairness:.3f}, "
                f"TargetAdj: {fm.target_hours_adjustment:+.3f}). Ranked #1 among {len(candidates)} "
                f"eligible candidate(s)."
            )
        if (
            template.role.name == "Bartender"
            and not selected.employee.employee_roles.filter(
                role__name="Server", active=True
            ).exists()
        ):
            reason += " Selected as bartender to preserve scarce cross-trained bar coverage."

        assignment = self._save_assignment(schedule_run, show, template, selected.employee, reason)

        # Save Fairness Snapshots for all evaluated candidates
        for c in candidates:
            cfm = fairness_map[c.employee.id]
            is_chosen = c.employee.id == selected.employee.id
            SchedulingFairnessSnapshot.objects.create(
                schedule_run=schedule_run,
                employee=c.employee,
                role=template.role,
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
                projected_hours=Decimal(str(round(cfm.projected_hours, 2))),
                reliability_score=Decimal(str(round(cfm.reliability_score, 2))),
                performance_score=Decimal(str(round(cfm.performance_score, 2))),
                role_fit_score=Decimal(str(round(cfm.role_fit_score, 2))),
                target_hours_adjustment=Decimal(str(round(cfm.target_hours_adjustment, 2))),
                confirmed_fair_score=Decimal(str(round(cfm.confirmed_fair_score, 3))),
                on_call_fair_score=Decimal(str(round(cfm.on_call_fair_score, 3))),
                selected=is_chosen,
                selection_reason=reason
                if is_chosen
                else f"Ranked lower than {selected.employee.display_name}",
                score_breakdown=cfm.breakdown,
            )

        return assignment

    def _assign_fifty_fifty(
        self,
        schedule_run: ScheduleRun,
        show: Show,
        template: ShiftTemplate,
        rotation: FiftyFiftyRotation,
    ) -> ScheduleAssignment | None:
        candidates, excluded = self._eligible_candidates(schedule_run, show, template)
        by_name = {
            candidate.employee.display_name: candidate
            for candidate in candidates
            if candidate.employee.display_name in {"Yana", "Kate"}
        }
        ordered = rotation.ordered_candidates(set(by_name))
        if not ordered:
            self._shortage(schedule_run, show, template, excluded)
            return None
        selected_name = ordered[0]
        selected = by_name[selected_name]
        rotation.record_assignment(selected_name, both_eligible=len(by_name) == 2)
        reason = (
            f"Selected for 50/50 under the Yana/Kate rotation; both eligible={len(by_name) == 2}."
        )
        return self._save_assignment(schedule_run, show, template, selected.employee, reason)

    def _save_assignment(
        self,
        schedule_run: ScheduleRun,
        show: Show,
        template: ShiftTemplate,
        employee: Employee,
        reason: str,
    ) -> ScheduleAssignment:
        start, end = self._datetimes(show, template)
        assignment = ScheduleAssignment(
            schedule_run=schedule_run,
            show=show,
            employee=employee,
            role=template.role,
            assignment_type=template.assignment_type,
            shift_template=template,
            start_datetime=start,
            end_datetime=end,
            scheduled_paid_hours=template.scheduled_paid_hours,
            on_call_hours=template.on_call_hours,
            selection_reason=reason,
        )
        assignment.full_clean()
        assignment.save()
        return assignment

    @staticmethod
    def _selection_reason(candidate: Candidate, template: ShiftTemplate) -> str:
        metrics = candidate.metrics
        parts = [
            "All hard constraints passed",
            f"opening/projected confirmed hours={metrics.confirmed_paid_hours}",
            f"confirmed shifts={metrics.confirmed_shift_count}",
            f"on-call assignments={metrics.on_call_assignment_count}",
            f"weekend assignments={metrics.weekend_assignment_count}",
            f"{template.role.name} capability={candidate.capability_level}",
        ]
        if candidate.employee.employment_priority:
            parts.append("Spirit-only confirmed-hours priority applied as a soft preference")
        return "; ".join(parts) + "."

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
                "Expected guests were not supplied; the default of 100 was used.",
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
