from datetime import date, time, timedelta
from decimal import Decimal

import pytest
from django.utils import timezone

from scheduling.models import (
    AssignmentType,
    AvailabilityType,
    Employee,
    EmployeeAvailability,
    EmployeeRole,
    EmployeeSchedulingPreference,
    Role,
    ScheduleAssignment,
    ScheduleRun,
    ScheduleRunStatus,
    SchedulingFairnessConfig,
    ShiftTemplate,
    Show,
)
from scheduling.services.engine import SchedulingEngine, shift_window_for
from scheduling.services.fairness import FairnessService


@pytest.fixture
def fairness_setup(db):
    config = SchedulingFairnessConfig.get_active_config()
    server_role, _ = Role.objects.get_or_create(name="Server")
    bartender_role, _ = Role.objects.get_or_create(name="Bartender")
    busser_role, _ = Role.objects.get_or_create(name="Busser")

    emp_l3 = Employee.objects.create(
        first_name="Level3",
        last_name="Server",
        display_name="Level3 Server",
        active=True,
    )
    EmployeeRole.objects.create(
        employee=emp_l3, role=server_role, capability_level=3, active=True
    )

    emp_l5 = Employee.objects.create(
        first_name="Level5",
        last_name="Server",
        display_name="Level5 Server",
        active=True,
    )
    EmployeeRole.objects.create(
        employee=emp_l5, role=server_role, capability_level=5, active=True
    )

    show = Show.objects.create(
        title="Test Show",
        date=date(2026, 9, 12),
        start_time=time(18, 30),
        end_time=time(22, 30),
        active=True,
    )

    template = ShiftTemplate.objects.create(
        code="server-1",
        name="Server 1",
        role=server_role,
        assignment_type=AssignmentType.CONFIRMED,
        start_time=time(17, 0),
        end_time=time(23, 0),
        scheduled_paid_hours=Decimal("6.00"),
    )

    return {
        "config": config,
        "role": server_role,
        "emp_l3": emp_l3,
        "emp_l5": emp_l5,
        "show": show,
        "template": template,
    }


@pytest.mark.django_db
def test_level_3_vs_level_5_fairness(fairness_setup):
    """Verify Level 3 Server with lower opportunity rate ranks ahead of Level 5 Server."""
    e_l3 = fairness_setup["emp_l3"]
    e_l5 = fairness_setup["emp_l5"]
    show = fairness_setup["show"]
    template = fairness_setup["template"]
    role = fairness_setup["role"]

    # Give Level 5 candidate 5 recent assignments, and Level 3 only 1 assignment
    run_prev = ScheduleRun.objects.create(
        start_date=date(2026, 8, 1),
        end_date=date(2026, 8, 30),
        status=ScheduleRunStatus.APPROVED,
    )

    for i in range(5):
        past_show = Show.objects.create(
            title=f"Past Show {i}",
            date=date(2026, 8, 10 + i),
            active=True,
        )
        ScheduleAssignment.objects.create(
            schedule_run=run_prev,
            show=past_show,
            employee=e_l5,
            role=role,
            assignment_type=AssignmentType.CONFIRMED,
            shift_template=template,
            start_datetime=timezone.now(),
            end_datetime=timezone.now(),
            scheduled_paid_hours=Decimal("6.00"),
            on_call_hours=Decimal("0.00"),
            selection_reason="Test assignment",
        )

    # Set availability for show date
    EmployeeAvailability.objects.create(
        employee=e_l3,
        date=show.date,
        availability_type=AvailabilityType.AVAILABLE_ALL_DAY,
    )
    EmployeeAvailability.objects.create(
        employee=e_l5,
        date=show.date,
        availability_type=AvailabilityType.AVAILABLE_ALL_DAY,
    )

    fairness = FairnessService()
    schedule_run = ScheduleRun.objects.create(
        start_date=date(2026, 9, 12), end_date=date(2026, 9, 12)
    )

    res = fairness.evaluate_candidates(
        [e_l3, e_l5],
        role,
        show,
        template,
        schedule_run,
        {e_l3.id: 3, e_l5.id: 5},
    )
    assert (
        res[e_l3.id].confirmed_fair_score > res[e_l5.id].confirmed_fair_score
    ), "Level 3 Server with lower opportunity history must rank ahead of Level 5 Server"


@pytest.mark.django_db
def test_opportunity_fairness():
    """Verify Candidate with lower opportunity rate ranks ahead of higher opportunity."""
    server_role, _ = Role.objects.get_or_create(name="Server")
    emp_a = Employee.objects.create(display_name="Candidate A", active=True)
    EmployeeRole.objects.create(employee=emp_a, role=server_role, capability_level=3, active=True)

    emp_b = Employee.objects.create(display_name="Candidate B", active=True)
    EmployeeRole.objects.create(employee=emp_b, role=server_role, capability_level=3, active=True)

    # Give A 5 recent assignments, B 1 recent assignment
    run_prev = ScheduleRun.objects.create(
        start_date=date(2026, 8, 1),
        end_date=date(2026, 8, 30),
        status=ScheduleRunStatus.APPROVED,
    )
    template = ShiftTemplate.objects.get_or_create(
        code="server-test",
        name="Server Test",
        role=server_role,
        assignment_type=AssignmentType.CONFIRMED,
        start_time=time(17, 0),
        end_time=time(23, 0),
        scheduled_paid_hours=Decimal("6.00"),
    )[0]

    for i in range(5):
        s = Show.objects.create(title=f"Show {i}", date=date(2026, 8, 1 + i), active=True)
        ScheduleAssignment.objects.create(
            schedule_run=run_prev,
            show=s,
            employee=emp_a,
            role=server_role,
            assignment_type=AssignmentType.CONFIRMED,
            shift_template=template,
            start_datetime=timezone.now(),
            end_datetime=timezone.now(),
            scheduled_paid_hours=Decimal("6.00"),
            on_call_hours=Decimal("0.00"),
        )
    s_b = Show.objects.create(title="Show B", date=date(2026, 8, 10), active=True)
    ScheduleAssignment.objects.create(
        schedule_run=run_prev,
        show=s_b,
        employee=emp_b,
        role=server_role,
        assignment_type=AssignmentType.CONFIRMED,
        shift_template=template,
        start_datetime=timezone.now(),
        end_datetime=timezone.now(),
        scheduled_paid_hours=Decimal("6.00"),
        on_call_hours=Decimal("0.00"),
    )

    fairness = FairnessService()
    show_now = Show.objects.create(title="Show Now", date=date(2026, 9, 12), active=True)
    schedule_run = ScheduleRun.objects.create(
        start_date=date(2026, 9, 12), end_date=date(2026, 9, 12)
    )

    res = fairness.evaluate_candidates(
        [emp_a, emp_b],
        server_role,
        show_now,
        template,
        schedule_run,
        {emp_a.id: 3, emp_b.id: 3},
    )
    assert res[emp_b.id].confirmed_fair_score > res[emp_a.id].confirmed_fair_score


@pytest.mark.django_db
def test_on_call_fairness():
    """Verify employee with fewer recent on-call shifts ranks higher for next on-call assignment."""
    bar_role, _ = Role.objects.get_or_create(name="Bartender")
    emp_joleen = Employee.objects.create(display_name="Joleen Test", active=True)
    EmployeeRole.objects.create(employee=emp_joleen, role=bar_role, capability_level=5, active=True)

    emp_svitlana = Employee.objects.create(display_name="Svitlana Test", active=True)
    EmployeeRole.objects.create(
        employee=emp_svitlana, role=bar_role, capability_level=4, active=True
    )

    run_prev = ScheduleRun.objects.create(
        start_date=date(2026, 8, 20),
        end_date=date(2026, 8, 30),
        status=ScheduleRunStatus.APPROVED,
    )
    template_oc = ShiftTemplate.objects.get_or_create(
        code="on-call-bartender-test",
        name="On-call Bartender Test",
        role=bar_role,
        assignment_type=AssignmentType.ON_CALL,
        start_time=time(17, 30),
        end_time=time(23, 0),
        on_call_hours=Decimal("5.50"),
    )[0]

    # Joleen gets 4 on-call assignments, Svitlana gets 1
    for i in range(4):
        s = Show.objects.create(title=f"OC Show {i}", date=date(2026, 8, 20 + i), active=True)
        ScheduleAssignment.objects.create(
            schedule_run=run_prev,
            show=s,
            employee=emp_joleen,
            role=bar_role,
            assignment_type=AssignmentType.ON_CALL,
            shift_template=template_oc,
            start_datetime=timezone.now(),
            end_datetime=timezone.now(),
            scheduled_paid_hours=Decimal("0.00"),
            on_call_hours=Decimal("5.50"),
        )
    s_s = Show.objects.create(title="OC Show S", date=date(2026, 8, 25), active=True)
    ScheduleAssignment.objects.create(
        schedule_run=run_prev,
        show=s_s,
        employee=emp_svitlana,
        role=bar_role,
        assignment_type=AssignmentType.ON_CALL,
        shift_template=template_oc,
        start_datetime=timezone.now(),
        end_datetime=timezone.now(),
        scheduled_paid_hours=Decimal("0.00"),
        on_call_hours=Decimal("5.50"),
    )

    fairness = FairnessService()
    show_now = Show.objects.create(title="Show Now", date=date(2026, 9, 12), active=True)
    schedule_run = ScheduleRun.objects.create(
        start_date=date(2026, 9, 12), end_date=date(2026, 9, 12)
    )

    res = fairness.evaluate_candidates(
        [emp_joleen, emp_svitlana],
        bar_role,
        show_now,
        template_oc,
        schedule_run,
        {emp_joleen.id: 5, emp_svitlana.id: 4},
    )
    assert res[emp_svitlana.id].on_call_fair_score > res[emp_joleen.id].on_call_fair_score


@pytest.mark.django_db
def test_olena_target_hours_priority():
    """Verify target hours adjustment boosts priority when below target."""
    server_role, _ = Role.objects.get_or_create(name="Server")
    olena = Employee.objects.create(
        display_name="Olena Test", active=True, spirit_only_employment=True
    )
    EmployeeRole.objects.create(employee=olena, role=server_role, capability_level=3, active=True)

    EmployeeSchedulingPreference.objects.create(
        employee=olena,
        target_hours=Decimal("40.00"),
        priority_enabled=True,
    )

    other = Employee.objects.create(display_name="Other Server", active=True)
    EmployeeRole.objects.create(employee=other, role=server_role, capability_level=3, active=True)

    fairness = FairnessService()
    show_now = Show.objects.create(title="Show Now", date=date(2026, 9, 12), active=True)
    schedule_run = ScheduleRun.objects.create(
        start_date=date(2026, 9, 12), end_date=date(2026, 9, 12)
    )
    template = ShiftTemplate.objects.get_or_create(
        code="server-test-2",
        name="Server Test 2",
        role=server_role,
        assignment_type=AssignmentType.CONFIRMED,
        start_time=time(17, 0),
        end_time=time(23, 0),
        scheduled_paid_hours=Decimal("6.00"),
    )[0]

    # When Olena has 0 hours (target 40), target_hours_adjustment should be positive (+0.10)
    res1 = fairness.evaluate_candidates(
        [olena, other],
        server_role,
        show_now,
        template,
        schedule_run,
        {olena.id: 3, other.id: 3},
    )
    assert res1[olena.id].target_hours_adjustment == 0.10

    # Now simulate Olena having 42 hours (above target 40)
    olena.opening_recent_hours = Decimal("42.00")
    olena.save()

    res2 = fairness.evaluate_candidates(
        [olena, other],
        server_role,
        show_now,
        template,
        schedule_run,
        {olena.id: 3, other.id: 3},
    )
    assert res2[olena.id].target_hours_adjustment == 0.00


@pytest.mark.django_db
def test_manager_exclusion_regression():
    """Verify Deborah Sweetapple and John Harris are excluded from automatic candidate ranking."""
    server_role, _ = Role.objects.get_or_create(name="Server")
    mgr1 = Employee.objects.create(display_name="Deborah Sweetapple", active=True)
    EmployeeRole.objects.create(employee=mgr1, role=server_role, capability_level=5, active=True)

    mgr2 = Employee.objects.create(display_name="John Harris", active=True)
    EmployeeRole.objects.create(employee=mgr2, role=server_role, capability_level=5, active=True)

    show = Show.objects.create(title="Show Mgr", date=date(2026, 9, 12), active=True)
    template = ShiftTemplate.objects.get_or_create(
        code="server-test-mgr",
        name="Server Test Mgr",
        role=server_role,
        assignment_type=AssignmentType.CONFIRMED,
        start_time=time(17, 0),
        end_time=time(23, 0),
        scheduled_paid_hours=Decimal("6.00"),
    )[0]

    EmployeeAvailability.objects.create(
        employee=mgr1, date=show.date, availability_type=AvailabilityType.AVAILABLE_ALL_DAY
    )
    EmployeeAvailability.objects.create(
        employee=mgr2, date=show.date, availability_type=AvailabilityType.AVAILABLE_ALL_DAY
    )

    engine = SchedulingEngine()
    schedule_run = ScheduleRun.objects.create(
        start_date=date(2026, 9, 12), end_date=date(2026, 9, 12)
    )
    candidates, excluded, _fitted = engine._eligible_candidates(schedule_run, show, template)

    candidate_names = [c.employee.display_name for c in candidates]
    assert "Deborah Sweetapple" not in candidate_names
    assert "John Harris" not in candidate_names
    assert "Deborah Sweetapple" in excluded
    assert "John Harris" in excluded


@pytest.mark.django_db
def test_partial_availability_is_fitted_rather_than_rejected():
    """Someone free for part of a shift takes that part, instead of being turned away.

    This used to assert the opposite: a window that did not cover the call time end to
    end was a hard no, which is how people with narrow availability ended up never
    being rostered at all. A human scheduler puts them on for the hours they have.
    """
    server_role, _ = Role.objects.get_or_create(name="Server")
    emp = Employee.objects.create(display_name="Partial Avail Server", active=True)
    EmployeeRole.objects.create(employee=emp, role=server_role, capability_level=3, active=True)

    show = Show.objects.create(title="Show Avail", date=date(2026, 9, 12), active=True)
    template = ShiftTemplate.objects.get_or_create(
        code="server-test-avail",
        name="Server Test Avail",
        role=server_role,
        assignment_type=AssignmentType.CONFIRMED,
        start_time=time(17, 0),
        end_time=time(23, 0),
        scheduled_paid_hours=Decimal("6.00"),
    )[0]

    # Partial window 17:30 - 21:30
    EmployeeAvailability.objects.create(
        employee=emp,
        date=show.date,
        availability_type=AvailabilityType.AVAILABLE_WINDOW,
        start_time=time(17, 30),
        end_time=time(21, 30),
    )

    engine = SchedulingEngine()
    schedule_run = ScheduleRun.objects.create(
        start_date=date(2026, 9, 12), end_date=date(2026, 9, 12)
    )
    candidates, excluded, fitted = engine._eligible_candidates(schedule_run, show, template)

    candidate_names = [c.employee.display_name for c in candidates]
    assert "Partial Avail Server" in candidate_names
    assert "Partial Avail Server" not in excluded

    # The window handed back sits inside her availability, never outside it.
    start, end = fitted[emp.id]
    assert start.time() >= time(17, 30)
    assert end.time() <= time(21, 30)
    assert (end - start).total_seconds() / 3600 >= 3.0


@pytest.mark.django_db
def test_an_overlap_too_short_to_be_worth_the_trip_is_still_refused():
    """Fitting is not a free pass: below the minimum the shift is not offered."""
    server_role, _ = Role.objects.get_or_create(name="Server")
    emp = Employee.objects.create(display_name="Barely Free Server", active=True)
    EmployeeRole.objects.create(employee=emp, role=server_role, capability_level=3, active=True)

    show = Show.objects.create(title="Show Short", date=date(2026, 9, 12), active=True)
    template = ShiftTemplate.objects.get_or_create(
        code="server-test-short",
        name="Server Test Short",
        role=server_role,
        assignment_type=AssignmentType.CONFIRMED,
        start_time=time(17, 0),
        end_time=time(23, 0),
        scheduled_paid_hours=Decimal("6.00"),
    )[0]
    start, end = shift_window_for(show, template)

    # Free for only the last 90 minutes of whatever window this shift works out to.
    EmployeeAvailability.objects.create(
        employee=emp,
        date=show.date,
        availability_type=AvailabilityType.AVAILABLE_WINDOW,
        start_time=(end - timedelta(minutes=90)).time(),
        end_time=end.time(),
    )

    engine = SchedulingEngine()
    schedule_run = ScheduleRun.objects.create(
        start_date=date(2026, 9, 12), end_date=date(2026, 9, 12)
    )
    candidates, excluded, _fitted = engine._eligible_candidates(schedule_run, show, template)

    assert "Barely Free Server" not in [c.employee.display_name for c in candidates]
    assert "Barely Free Server" in excluded
