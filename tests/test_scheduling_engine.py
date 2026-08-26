from datetime import date, time, timedelta
from decimal import Decimal

import pytest
from django.core.exceptions import ValidationError
from django.core.management import call_command
from django.utils import timezone

from scheduling.models import (
    MINIMUM_VIABLE_GUESTS,
    AssignmentType,
    AvailabilityType,
    Employee,
    EmployeeAvailability,
    EmployeeRole,
    FiftyFiftyRotationConfig,
    OfficeAssignment,
    OfficeRotationConfig,
    Role,
    ScheduleRunStatus,
    Show,
    WarningSeverity,
    WarningType,
)
from scheduling.services.engine import ApprovedScheduleError, SchedulingEngine
from scheduling.services.requirements import (
    staffing_requirements_for,
    templates_for_requirement,
)


@pytest.fixture
def configured_staff(db):
    call_command("seed_spirit_staff", verbosity=0)
    call_command("seed_scheduling_config", verbosity=0)
    return list(Employee.objects.filter(active=True))


def make_show(show_date=date(2026, 9, 12), **overrides):
    # 80 guests is the buffer management plans against, and the bottom rung of the
    # staffing ladder (3 confirmed servers, 1 on-call, 1 bartender + 1 on-call, 1 busser).
    values = {
        "title": f"Test Show {show_date}",
        "date": show_date,
        "expected_guests": 80,
        "requires_50_50": True,
    }
    values.update(overrides)
    return Show.objects.create(**values)


def make_all_available(employees, *dates):
    for employee in employees:
        for show_date in dates:
            EmployeeAvailability.objects.create(
                employee=employee,
                date=show_date,
                availability_type=AvailabilityType.AVAILABLE_ALL_DAY,
            )


@pytest.mark.django_db
def test_standard_buffer_show_has_required_coverage(configured_staff):
    """The 75-99 guest band: the crew management runs on an ordinary night."""
    show = make_show()
    make_all_available(configured_staff, show.date)
    run = SchedulingEngine().generate(show.date, show.date)

    def count(role, assignment_type=None):
        assignments = run.assignments.filter(role__name=role)
        if assignment_type:
            assignments = assignments.filter(assignment_type=assignment_type)
        return assignments.count()

    assert count("Server", AssignmentType.CONFIRMED) == 3
    assert count("Server", AssignmentType.ON_CALL) == 1
    assert count("Bartender", AssignmentType.CONFIRMED) == 1
    assert count("Bartender", AssignmentType.ON_CALL) == 1
    assert run.assignments.filter(role__name="Busser").count() == 1
    assert run.assignments.filter(assignment_type=AssignmentType.FIFTY_FIFTY).count() == 1
    assert run.assignments.values("employee_id").distinct().count() == 8

    # Shift windows anchor to this show's own doors (18:30) and wrap (22:30), so the Lead
    # Server comes in 45 minutes before doors and leaves 15 minutes after wrap.
    lead = run.assignments.get(shift_template__code="lead-server")
    assert timezone.localtime(lead.start_datetime).time() == time(17, 45)
    assert timezone.localtime(lead.end_datetime).time() == time(22, 45)

    # On-call carries no setup buffer - exactly doors to wrap - and no paid hours.
    on_call = run.assignments.get(shift_template__code="on-call-server")
    assert timezone.localtime(on_call.start_datetime).time() == time(18, 30)
    assert timezone.localtime(on_call.end_datetime).time() == time(22, 30)
    assert on_call.scheduled_paid_hours == 0
    assert on_call.on_call_hours == Decimal("4.00")

    assert run.status == ScheduleRunStatus.GENERATED


@pytest.mark.django_db
def test_staffing_scales_by_the_coverage_ratios(configured_staff):
    """One server per 25 guests, one bartender per 75, one busser per 100.

    Each block stretches five guests further before another person is added - nobody
    is called in for the sake of five more covers.
    """
    expected = {
        30: (1, 1, 1),   # a block covers its ratio plus the five-guest buffer
        31: (2, 1, 1),
        80: (3, 1, 1),   # the standing crew: buffer at the top of the 75-guest block
        81: (4, 2, 1),
        105: (4, 2, 1),
        106: (5, 2, 2),
        175: (7, 3, 2),  # full house
    }
    for index, (guests, (servers, bartenders, bussers)) in enumerate(expected.items()):
        show = make_show(
            date(2026, 9, 12) + timedelta(days=index * 7),
            expected_guests=guests,
            requires_50_50=False,
        )
        requirements, over_capacity = staffing_requirements_for(show)
        assert not over_capacity, f"{guests} guests wrongly flagged over capacity"
        actual = {r.role_name: r.confirmed_count for r in requirements}
        assert actual["Server"] == servers, f"{guests} guests"
        assert actual["Bartender"] == bartenders, f"{guests} guests"
        assert actual["Busser"] == bussers, f"{guests} guests"
        for requirement in requirements:
            available = len(templates_for_requirement(requirement))
            needed = requirement.confirmed_count + requirement.on_call_count
            assert available == needed, f"{guests} guests: {requirement.role_name}"


@pytest.mark.django_db
def test_every_role_keeps_at_least_one_person(configured_staff):
    """A show that runs needs somebody in each role, however small the house."""
    show = make_show(expected_guests=MINIMUM_VIABLE_GUESTS, requires_50_50=False)
    counts = {r.role_name: r.confirmed_count for r in staffing_requirements_for(show)[0]}
    assert counts["Server"] == 3
    assert counts["Bartender"] == 1
    assert counts["Busser"] == 1


@pytest.mark.django_db
def test_show_below_viability_threshold_is_not_staffed(configured_staff):
    """A show under 75 guests is cancelled or its guests moved - never staffed."""
    show = make_show(expected_guests=MINIMUM_VIABLE_GUESTS - 1)
    make_all_available(configured_staff, show.date)
    run = SchedulingEngine().generate(show.date, show.date, allow_shortages=True)

    assert not run.assignments.exists()
    warning = run.warnings.get(warning_type=WarningType.EVENT_STAFFING_REVIEW_REQUIRED)
    assert warning.message.startswith("BELOW_VIABILITY_THRESHOLD")


@pytest.mark.django_db
def test_show_that_runs_below_three_confirmed_servers_escalates(configured_staff):
    """Three confirmed servers is a floor, not a target: a breach blocks the run."""
    show = make_show(requires_50_50=False)
    make_all_available(configured_staff, show.date)
    # Leave exactly one server-qualified employee available.
    keep = Employee.objects.get(display_name="Olena")
    EmployeeRole.objects.filter(role__name="Server").exclude(employee=keep).update(active=False)

    run = SchedulingEngine().generate(show.date, show.date, allow_shortages=True)

    breach = run.warnings.filter(
        warning_type=WarningType.SERVER_SHORTAGE,
        message__startswith="BELOW_SERVER_FLOOR",
        severity=WarningSeverity.ERROR,
    )
    assert breach.exists()
    assert run.status == ScheduleRunStatus.NEEDS_REVIEW


@pytest.mark.django_db
@pytest.mark.parametrize(
    "availability_type",
    [AvailabilityType.UNAVAILABLE, AvailabilityType.UNKNOWN],
)
def test_unavailable_or_unknown_employee_is_never_scheduled(configured_staff, availability_type):
    show = make_show(requires_50_50=False)
    make_all_available(configured_staff, show.date)
    olena = Employee.objects.get(display_name="Olena")
    entry = EmployeeAvailability.objects.get(employee=olena, date=show.date)
    entry.availability_type = availability_type
    entry.save()
    run = SchedulingEngine().generate(show.date, show.date, allow_shortages=True)
    assert not run.assignments.filter(employee=olena).exists()
    # The floor still holds: losing one person is covered from the rest of the pool.
    assert (
        run.assignments.filter(
            role__name="Server", assignment_type=AssignmentType.CONFIRMED
        ).count()
        == 3
    )


@pytest.mark.django_db
def test_role_qualification_is_a_hard_constraint(configured_staff):
    show = make_show(requires_50_50=False)
    make_all_available(configured_staff, show.date)
    server_role = Role.objects.get(name="Server")
    EmployeeRole.objects.filter(role=server_role).update(active=False)
    run = SchedulingEngine().generate(show.date, show.date, allow_shortages=True)
    assert not run.assignments.filter(role=server_role).exists()
    # One shortage warning per unfillable confirmed server position, plus the floor breach.
    assert (
        run.warnings.filter(warning_type=WarningType.SERVER_SHORTAGE)
        .exclude(message__startswith="BELOW_SERVER_FLOOR")
        .count()
        == 3
    )
    assert run.warnings.filter(message__startswith="BELOW_SERVER_FLOOR").count() == 1


@pytest.mark.django_db
def test_excluded_manager_is_never_automatically_scheduled(configured_staff):
    show = make_show(requires_50_50=False)
    make_all_available(configured_staff, show.date)
    manager = Employee.objects.create(
        first_name="John",
        last_name="Haris",
        display_name="John Haris",
    )
    EmployeeRole.objects.create(
        employee=manager,
        role=Role.objects.get(name="Server"),
        capability_level=5,
    )
    EmployeeAvailability.objects.create(
        employee=manager,
        date=show.date,
        availability_type=AvailabilityType.AVAILABLE_ALL_DAY,
    )
    run = SchedulingEngine().generate(show.date, show.date)
    assert not run.assignments.filter(employee=manager).exists()


@pytest.mark.django_db
def test_bartender_positions_are_protected_before_server_assignment(configured_staff):
    show = make_show(requires_50_50=False)
    make_all_available(configured_staff, show.date)
    run = SchedulingEngine().generate(show.date, show.date)
    bartender_names = set(
        run.assignments.filter(role__name="Bartender").values_list(
            "employee__display_name", flat=True
        )
    )
    assert bartender_names.isdisjoint({"Jackie Pynn", "Joleen Dickson", "Svitlana"})
    assert run.assignments.filter(role__name="Bartender").count() == 2


@pytest.mark.django_db
def test_insufficient_bartenders_create_warnings_not_invalid_assignments(configured_staff):
    show = make_show(requires_50_50=False)
    make_all_available(configured_staff, show.date)
    EmployeeRole.objects.filter(role__name="Bartender").update(active=False)
    run = SchedulingEngine().generate(show.date, show.date, allow_shortages=True)
    assert not run.assignments.filter(role__name="Bartender").exists()
    assert run.warnings.filter(warning_type=WarningType.BARTENDER_SHORTAGE).count() == 1
    assert run.warnings.filter(warning_type=WarningType.ON_CALL_BARTENDER_SHORTAGE).count() == 1


@pytest.mark.django_db
def test_yana_kate_rotation_alternates_and_prevents_double_role(configured_staff):
    first = make_show(date(2026, 9, 12))
    second = make_show(date(2026, 9, 18))
    make_all_available(configured_staff, first.date, second.date)
    FiftyFiftyRotationConfig.objects.create(seed_employee=Employee.objects.get(display_name="Yana"))
    run = SchedulingEngine().generate(first.date, second.date)
    fifty = list(
        run.assignments.filter(assignment_type=AssignmentType.FIFTY_FIFTY)
        .order_by("show__date")
        .values_list("employee__display_name", flat=True)
    )
    assert fifty == ["Yana", "Kate"]
    for assignment in run.assignments.filter(assignment_type=AssignmentType.FIFTY_FIFTY):
        assert (
            run.assignments.filter(show=assignment.show, employee=assignment.employee).count() == 1
        )


@pytest.mark.django_db
def test_fifty_fifty_rotation_pauses_when_one_person_is_unavailable(configured_staff):
    first = make_show(date(2026, 9, 12))
    second = make_show(date(2026, 9, 18))
    make_all_available(configured_staff, first.date, second.date)
    yana = Employee.objects.get(display_name="Yana")
    entry = EmployeeAvailability.objects.get(employee=yana, date=first.date)
    entry.availability_type = AvailabilityType.UNAVAILABLE
    entry.save()
    FiftyFiftyRotationConfig.objects.create(seed_employee=yana)
    run = SchedulingEngine().generate(first.date, second.date)
    fifty = list(
        run.assignments.filter(assignment_type=AssignmentType.FIFTY_FIFTY)
        .order_by("show__date")
        .values_list("employee__display_name", flat=True)
    )
    assert fifty == ["Kate", "Yana"]


@pytest.mark.django_db
def test_office_conflict_only_applies_when_times_overlap(configured_staff):
    show = make_show(requires_50_50=False)
    make_all_available(configured_staff, show.date)
    olena = Employee.objects.get(display_name="Olena")
    # Server 1 runs 17:45-22:45 (45 minutes before this show's 18:30 doors), so the
    # office block has to reach past 17:45 to actually clash with it.
    OfficeAssignment.objects.create(
        employee=olena,
        date=show.date,
        start_time=time(9),
        end_time=time(18, 30),
    )
    run = SchedulingEngine().generate(show.date, show.date)
    assert not run.assignments.filter(employee=olena, shift_template__code="lead-server").exists()
    # A shift beginning exactly when office work ends does not overlap.
    if run.assignments.filter(employee=olena).exists():
        assert run.assignments.get(employee=olena).start_datetime.time() >= time(17)


@pytest.mark.django_db
def test_priority_never_overrides_unavailability_or_qualification(configured_staff):
    show = make_show(requires_50_50=False)
    make_all_available(configured_staff, show.date)
    olena = Employee.objects.get(display_name="Olena")
    EmployeeAvailability.objects.filter(employee=olena, date=show.date).update(
        availability_type=AvailabilityType.UNAVAILABLE,
        available=False,
    )
    EmployeeRole.objects.filter(employee=olena, role__name="Server").update(active=False)
    run = SchedulingEngine().generate(show.date, show.date)
    assert not run.assignments.filter(employee=olena).exists()


@pytest.mark.django_db
def test_olena_and_jackie_receive_soft_confirmed_opportunity(configured_staff):
    show = make_show(requires_50_50=False)
    make_all_available(configured_staff, show.date)
    run = SchedulingEngine().generate(show.date, show.date)
    confirmed_server_names = set(
        run.assignments.filter(
            role__name="Server",
            assignment_type=AssignmentType.CONFIRMED,
        ).values_list("employee__display_name", flat=True)
    )
    assert {"Olena", "Jackie Pynn"}.issubset(confirmed_server_names)


@pytest.mark.django_db
def test_approved_schedule_cannot_be_regenerated(configured_staff):
    show = make_show()
    make_all_available(configured_staff, show.date)
    run = SchedulingEngine().generate(show.date, show.date)
    run.status = ScheduleRunStatus.APPROVED
    run.save(update_fields=["status"])
    with pytest.raises(ApprovedScheduleError):
        SchedulingEngine().generate(show.date, show.date, schedule_run=run)


@pytest.mark.django_db
def test_a_guest_count_beyond_capacity_is_flagged_for_review(configured_staff):
    """Staffing is computed from the ratios at any count, so only an impossible house
    needs management review: more guests than the room holds."""
    inside = make_show(date(2026, 9, 12), expected_guests=120, requires_50_50=False)
    make_all_available(configured_staff, inside.date)
    run = SchedulingEngine().generate(inside.date, inside.date, allow_shortages=True)
    assert not run.warnings.filter(warning_type=WarningType.HIGH_GUEST_COUNT_REVIEW).exists()

    beyond = make_show(
        date(2026, 9, 19), expected_guests=200, capacity=175, requires_50_50=False
    )
    make_all_available(configured_staff, beyond.date)
    run = SchedulingEngine().generate(beyond.date, beyond.date, allow_shortages=True)
    assert run.warnings.filter(warning_type=WarningType.HIGH_GUEST_COUNT_REVIEW).exists()
    assert run.status == ScheduleRunStatus.NEEDS_REVIEW


@pytest.mark.django_db
def test_manual_assignment_model_rejects_on_call_paid_hours(configured_staff):
    show = make_show()
    make_all_available(configured_staff, show.date)
    run = SchedulingEngine().generate(show.date, show.date)
    assignment = run.assignments.filter(assignment_type=AssignmentType.ON_CALL).first()
    assignment.scheduled_paid_hours = 1
    with pytest.raises(ValidationError):
        assignment.full_clean()


@pytest.mark.django_db
def test_lower_recent_hours_win_confirmed_work_before_on_call(configured_staff):
    show = make_show(requires_50_50=False)
    make_all_available(configured_staff, show.date)
    Employee.objects.filter(employee_roles__role__name="Server").update(opening_recent_hours=100)
    molly = Employee.objects.get(display_name="Molly Rittwage")
    molly.opening_recent_hours = 0
    molly.save(update_fields=["opening_recent_hours"])
    run = SchedulingEngine().generate(show.date, show.date)
    assignment = run.assignments.get(employee=molly)
    assert assignment.assignment_type == AssignmentType.CONFIRMED


@pytest.mark.django_db
def test_on_call_burden_is_distributed_deterministically(configured_staff):
    dates = [date(2026, 9, 12), date(2026, 9, 18), date(2026, 9, 19), date(2026, 9, 25)]
    for show_date in dates:
        make_show(show_date, requires_50_50=False)
    make_all_available(configured_staff, *dates)
    run = SchedulingEngine().generate(dates[0], dates[-1])
    counts = {}
    for name in run.assignments.filter(assignment_type=AssignmentType.ON_CALL).values_list(
        "employee__display_name", flat=True
    ):
        counts[name] = counts.get(name, 0) + 1
    assert max(counts.values()) <= 1


@pytest.mark.django_db
def test_weekend_office_rotation_alternates(configured_staff):
    yana = Employee.objects.get(display_name="Yana")
    OfficeRotationConfig.objects.create(
        seed_date=date(2026, 9, 12),
        seed_saturday_employee=yana,
    )
    dates = [date(2026, 9, 12), date(2026, 9, 13), date(2026, 9, 19), date(2026, 9, 20)]
    for show_date in dates:
        make_show(show_date, requires_50_50=False)
    make_all_available(configured_staff, *dates)
    SchedulingEngine().generate(dates[0], dates[-1])
    assignments = list(
        OfficeAssignment.objects.order_by("date").values_list("employee__display_name", flat=True)
    )
    assert assignments == ["Yana", "Khrystyna", "Khrystyna", "Yana"]


@pytest.mark.django_db
def test_office_warning_only_when_shift_times_overlap(configured_staff):
    show = make_show(requires_50_50=False)
    make_all_available(configured_staff, show.date)
    yana = Employee.objects.get(display_name="Yana")
    OfficeAssignment.objects.create(
        employee=yana,
        date=show.date,
        start_time=time(9),
        end_time=time(15),
    )
    run = SchedulingEngine().generate(show.date, show.date)
    assert not run.warnings.filter(warning_type=WarningType.OFFICE_CONFLICT).exists()


@pytest.mark.django_db
def test_weekend_busser_work_is_distributed_before_repeating(configured_staff):
    dates = [date(2026, 9, 12), date(2026, 9, 19), date(2026, 9, 26)]
    for show_date in dates:
        make_show(show_date, requires_50_50=False)
    make_all_available(configured_staff, *dates)
    run = SchedulingEngine().generate(dates[0], dates[-1])
    busser_names = list(
        run.assignments.filter(role__name="Busser")
        .order_by("show__date")
        .values_list("employee__display_name", flat=True)
    )
    assert len(set(busser_names)) == 3


@pytest.mark.django_db
def test_schedule_generation_is_deterministic(configured_staff):
    show = make_show()
    make_all_available(configured_staff, show.date)
    first = SchedulingEngine().generate(show.date, show.date)
    second = SchedulingEngine().generate(show.date, show.date)
    first_assignments = list(
        first.assignments.order_by("shift_template__position_order").values_list(
            "shift_template__code", "employee__display_name"
        )
    )
    second_assignments = list(
        second.assignments.order_by("shift_template__position_order").values_list(
            "shift_template__code", "employee__display_name"
        )
    )
    assert first_assignments == second_assignments
