"""
Fairness Service for the Spirit of Newfoundland Scheduling Engine.

Calculates normalized fairness scores (0.00 to 1.00) for confirmed shifts and on-call assignments,
tracking eligible opportunities, actual hours, role-specific opportunities, weekend burden,
rest/consecutive-night burden, Spirit-only target hours adjustments, and close tie-breakers.
"""

from dataclasses import dataclass, field
from datetime import timedelta

from scheduling.models import (
    AssignmentType,
    Employee,
    Role,
    ScheduleAssignment,
    ScheduleRun,
    ScheduleRunStatus,
    SchedulingFairnessConfig,
    ShiftTemplate,
    Show,
)


@dataclass
class CandidateFairnessMetrics:
    employee: Employee
    eligible_opportunities: int = 0
    confirmed_opportunities: int = 0
    opportunity_rate: float = 0.0
    opportunity_fairness: float = 0.0

    recent_actual_hours: float = 0.0
    hours_fairness: float = 0.0

    role_opportunities: int = 0
    role_opportunity_fairness: float = 0.0

    recent_confirmed_shifts: int = 0
    confirmed_shift_fairness: float = 0.0

    recent_weekend_shifts: int = 0
    weekend_fairness: float = 0.0

    consecutive_nights: int = 0
    rest_fairness: float = 1.0

    recent_on_call_assignments: int = 0
    on_call_count_fairness: float = 1.0

    recent_on_call_hours: float = 0.0
    on_call_hours_fairness: float = 1.0

    reliability_score: float = 0.50
    performance_score: float = 0.50
    role_fit_score: float = 0.50

    target_hours: float | None = None
    projected_hours: float = 0.0
    target_hours_adjustment: float = 0.0

    confirmed_fair_score: float = 0.0
    on_call_fair_score: float = 0.0

    breakdown: dict[str, float] = field(default_factory=dict)


def inverse_normalize(val: float, min_val: float, max_val: float) -> float:
    """Higher raw value gives lower score (inverse normalization)."""
    if max_val <= min_val:
        return 1.0
    score = 1.0 - ((val - min_val) / (max_val - min_val))
    return max(0.0, min(1.0, score))


def direct_normalize(val: float, min_val: float, max_val: float) -> float:
    """Higher raw value gives higher score."""
    if max_val <= min_val:
        return 0.5
    score = (val - min_val) / (max_val - min_val)
    return max(0.0, min(1.0, score))


class FairnessService:
    def __init__(self, config: SchedulingFairnessConfig | None = None):
        self.config = config or SchedulingFairnessConfig.get_active_config()

    def evaluate_candidates(
        self,
        candidates: list[Employee],
        role: Role,
        show: Show,
        template: ShiftTemplate,
        schedule_run: ScheduleRun,
        capability_levels: dict[int, int],
        eligibility_service=None,
    ) -> dict[int, CandidateFairnessMetrics]:
        """
        Computes complete fairness metrics and scores for all eligible candidates for a position.
        """
        metrics_by_emp_id: dict[int, CandidateFairnessMetrics] = {}
        show_date = show.date
        config = self.config

        hours_start = show_date - timedelta(days=config.recent_hours_window_days)
        shifts_start = show_date - timedelta(days=config.confirmed_shift_window_days)
        on_call_start = show_date - timedelta(days=config.on_call_window_days)
        weekend_start = show_date - timedelta(days=config.weekend_window_days)

        prior_shows = list(
            Show.objects.filter(
                active=True,
                requires_service_staff=True,
                date__range=(shifts_start, show_date - timedelta(days=1)),
            )
        )

        for emp in candidates:
            m = CandidateFairnessMetrics(employee=emp)

            # 1. Eligible Opportunities & Confirmed Opportunities in window
            eligible_cnt = 0
            confirmed_cnt = 0
            for past_show in prior_shows:
                had_assign = ScheduleAssignment.objects.filter(
                    schedule_run__status__in=[
                        ScheduleRunStatus.GENERATING,
                        ScheduleRunStatus.APPROVED,
                        ScheduleRunStatus.SYNCED_TO_SQUARE,
                    ],
                    employee=emp,
                    show=past_show,
                    assignment_type=AssignmentType.CONFIRMED,
                ).exists()

                if had_assign:
                    confirmed_cnt += 1
                    eligible_cnt += 1
                elif eligibility_service:
                    res = eligibility_service.evaluate_simple(emp, past_show)
                    if res:
                        eligible_cnt += 1

            if eligible_cnt == 0:
                m.eligible_opportunities = 1
                m.confirmed_opportunities = 0
                m.opportunity_rate = 0.0
            else:
                m.eligible_opportunities = eligible_cnt
                m.confirmed_opportunities = confirmed_cnt
                m.opportunity_rate = round(confirmed_cnt / eligible_cnt, 4)

            # 2. Recent Actual Paid Hours
            past_assigns = ScheduleAssignment.objects.filter(
                schedule_run__status__in=[
                    ScheduleRunStatus.GENERATING,
                    ScheduleRunStatus.APPROVED,
                    ScheduleRunStatus.SYNCED_TO_SQUARE,
                ],
                employee=emp,
                show__date__range=(hours_start, show_date - timedelta(days=1)),
                assignment_type=AssignmentType.CONFIRMED,
            )
            worked_hrs = float(sum(a.scheduled_paid_hours for a in past_assigns))
            current_run_assigns = ScheduleAssignment.objects.filter(
                schedule_run=schedule_run,
                employee=emp,
                assignment_type=AssignmentType.CONFIRMED,
            )
            current_run_hrs = float(sum(a.scheduled_paid_hours for a in current_run_assigns))
            total_hours = worked_hrs + current_run_hrs + float(emp.opening_recent_hours)
            m.recent_actual_hours = round(total_hours, 2)
            m.projected_hours = round(total_hours, 2)

            # 3. Role-Specific Opportunities
            m.role_opportunities = ScheduleAssignment.objects.filter(
                schedule_run__status__in=[
                    ScheduleRunStatus.GENERATING,
                    ScheduleRunStatus.APPROVED,
                    ScheduleRunStatus.SYNCED_TO_SQUARE,
                ],
                employee=emp,
                role=role,
                assignment_type=AssignmentType.CONFIRMED,
                show__date__range=(shifts_start, show_date - timedelta(days=1)),
            ).count()

            # 4. Recent Confirmed Shift Count
            m.recent_confirmed_shifts = past_assigns.count() + current_run_assigns.count()

            # 5. Weekend Shifts (Fri/Sat/Sun)
            m.recent_weekend_shifts = ScheduleAssignment.objects.filter(
                schedule_run__status__in=[
                    ScheduleRunStatus.GENERATING,
                    ScheduleRunStatus.APPROVED,
                    ScheduleRunStatus.SYNCED_TO_SQUARE,
                ],
                employee=emp,
                show__date__range=(weekend_start, show_date - timedelta(days=1)),
                show__date__week_day__in=[1, 6, 7],
                assignment_type=AssignmentType.CONFIRMED,
            ).count()

            # 6. Consecutive Previous Nights
            consec = 0
            check_date = show_date - timedelta(days=1)
            while True:
                had_night = ScheduleAssignment.objects.filter(
                    schedule_run__status__in=[
                        ScheduleRunStatus.GENERATING,
                        ScheduleRunStatus.APPROVED,
                        ScheduleRunStatus.SYNCED_TO_SQUARE,
                    ],
                    employee=emp,
                    show__date=check_date,
                    assignment_type=AssignmentType.CONFIRMED,
                ).exists()
                if had_night:
                    consec += 1
                    check_date -= timedelta(days=1)
                else:
                    break
            m.consecutive_nights = consec
            if consec == 0:
                m.rest_fairness = 1.0
            elif consec == 1:
                m.rest_fairness = 0.8
            elif consec == 2:
                m.rest_fairness = 0.5
            else:
                m.rest_fairness = 0.2

            # 7. Recent On-Call Assignments & Hours
            on_call_assigns = ScheduleAssignment.objects.filter(
                schedule_run__status__in=[
                    ScheduleRunStatus.GENERATING,
                    ScheduleRunStatus.APPROVED,
                    ScheduleRunStatus.SYNCED_TO_SQUARE,
                ],
                employee=emp,
                show__date__range=(on_call_start, show_date - timedelta(days=1)),
                assignment_type=AssignmentType.ON_CALL,
            )
            current_on_call = ScheduleAssignment.objects.filter(
                schedule_run=schedule_run,
                employee=emp,
                assignment_type=AssignmentType.ON_CALL,
            )
            m.recent_on_call_assignments = on_call_assigns.count() + current_on_call.count()
            m.recent_on_call_hours = float(
                sum(a.on_call_hours for a in on_call_assigns)
                + sum(a.on_call_hours for a in current_on_call)
            )

            # 8. Reliability & Performance
            m.reliability_score = float(config.default_reliability)
            m.performance_score = float(config.default_performance)

            # 9. Capability Level & Role Fit
            # Capability is a small tie-break on competence, not seniority. The first
            # server position used to score Level 5 far above Level 3 as a stand-in for
            # a lead grade; there is no such grade - Square holds one "Service" job and
            # the published rosters put Level 3 staff on the earliest, longest shifts.
            cap_level = capability_levels.get(emp.id, 3)
            m.role_fit_score = {5: 0.75, 4: 0.75, 3: 0.75, 2: 0.50, 1: 0.25}.get(
                cap_level, 0.50
            )

            # 10. Target Hours Adjustment
            pref = getattr(emp, "scheduling_preference", None)
            target_hrs = float(pref.target_hours) if pref and pref.target_hours else None
            m.target_hours = target_hrs
            if target_hrs is not None and pref and pref.priority_enabled:
                deficit = target_hrs - m.projected_hours
                if deficit > 10.0:
                    m.target_hours_adjustment = 0.10
                elif deficit > 0.0:
                    m.target_hours_adjustment = 0.05
                else:
                    m.target_hours_adjustment = 0.00
            elif emp.spirit_only_employment or emp.employment_priority > 0:
                m.target_hours_adjustment = 0.15
            else:
                m.target_hours_adjustment = 0.00

            metrics_by_emp_id[emp.id] = m

        # Pool Normalization across candidates
        if candidates:
            opp_rates = [m.opportunity_rate for m in metrics_by_emp_id.values()]
            min_opp, max_opp = min(opp_rates), max(opp_rates)
            for m in metrics_by_emp_id.values():
                m.opportunity_fairness = inverse_normalize(m.opportunity_rate, min_opp, max_opp)

            hrs_list = [m.recent_actual_hours for m in metrics_by_emp_id.values()]
            min_h, max_h = min(hrs_list), max(hrs_list)
            for m in metrics_by_emp_id.values():
                m.hours_fairness = inverse_normalize(m.recent_actual_hours, min_h, max_h)

            role_opps = [m.role_opportunities for m in metrics_by_emp_id.values()]
            min_ro, max_ro = min(role_opps), max(role_opps)
            for m in metrics_by_emp_id.values():
                m.role_opportunity_fairness = inverse_normalize(
                    m.role_opportunities, min_ro, max_ro
                )

            shift_cnts = [m.recent_confirmed_shifts for m in metrics_by_emp_id.values()]
            min_s, max_s = min(shift_cnts), max(shift_cnts)
            for m in metrics_by_emp_id.values():
                m.confirmed_shift_fairness = inverse_normalize(
                    m.recent_confirmed_shifts, min_s, max_s
                )

            wknd_cnts = [m.recent_weekend_shifts for m in metrics_by_emp_id.values()]
            min_w, max_w = min(wknd_cnts), max(wknd_cnts)
            for m in metrics_by_emp_id.values():
                m.weekend_fairness = inverse_normalize(m.recent_weekend_shifts, min_w, max_w)

            oc_cnts = [m.recent_on_call_assignments for m in metrics_by_emp_id.values()]
            min_oc, max_oc = min(oc_cnts), max(oc_cnts)

            oc_hrs = [m.recent_on_call_hours for m in metrics_by_emp_id.values()]
            min_och, max_och = min(oc_hrs), max(oc_hrs)

            for m in metrics_by_emp_id.values():
                m.on_call_count_fairness = inverse_normalize(
                    m.recent_on_call_assignments, min_oc, max_oc
                )
                m.on_call_hours_fairness = inverse_normalize(
                    m.recent_on_call_hours, min_och, max_och
                )

        # Compute Final Scores
        for m in metrics_by_emp_id.values():
            if template.assignment_type == AssignmentType.ON_CALL:
                on_call_score = (
                    float(config.on_call_count_weight) * m.on_call_count_fairness
                    + float(config.on_call_hours_weight) * m.on_call_hours_fairness
                    + float(config.on_call_opportunity_weight) * m.opportunity_fairness
                    + float(config.on_call_confirmed_workload_weight) * m.hours_fairness
                    + float(config.on_call_weekend_weight) * m.weekend_fairness
                    + float(config.on_call_reliability_weight) * m.reliability_score
                    + float(config.on_call_role_fit_weight) * m.role_fit_score
                )
                m.on_call_fair_score = round(on_call_score, 3)
                m.breakdown = {
                    "on_call_count_fairness": round(m.on_call_count_fairness, 3),
                    "on_call_hours_fairness": round(m.on_call_hours_fairness, 3),
                    "opportunity_fairness": round(m.opportunity_fairness, 3),
                    "hours_fairness": round(m.hours_fairness, 3),
                    "weekend_fairness": round(m.weekend_fairness, 3),
                    "reliability": round(m.reliability_score, 3),
                    "role_fit": round(m.role_fit_score, 3),
                }
            else:
                confirmed_score = (
                    float(config.opportunity_weight) * m.opportunity_fairness
                    + float(config.hours_weight) * m.hours_fairness
                    + float(config.role_opportunity_weight) * m.role_opportunity_fairness
                    + float(config.confirmed_shift_weight) * m.confirmed_shift_fairness
                    + float(config.weekend_weight) * m.weekend_fairness
                    + float(config.rest_weight) * m.rest_fairness
                    + float(config.reliability_weight) * m.reliability_score
                    + float(config.performance_weight) * m.performance_score
                    + float(config.role_fit_weight) * m.role_fit_score
                    + m.target_hours_adjustment
                )
                m.confirmed_fair_score = round(confirmed_score, 3)
                m.breakdown = {
                    "opportunity_fairness": round(m.opportunity_fairness, 3),
                    "hours_fairness": round(m.hours_fairness, 3),
                    "role_opportunity_fairness": round(m.role_opportunity_fairness, 3),
                    "confirmed_shift_fairness": round(m.confirmed_shift_fairness, 3),
                    "weekend_fairness": round(m.weekend_fairness, 3),
                    "rest_fairness": round(m.rest_fairness, 3),
                    "reliability": round(m.reliability_score, 3),
                    "performance": round(m.performance_score, 3),
                    "role_fit": round(m.role_fit_score, 3),
                    "target_hours_adjustment": round(m.target_hours_adjustment, 3),
                }

        return metrics_by_emp_id
