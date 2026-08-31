from datetime import date, time, timedelta
from decimal import Decimal

import pytest
from django.core.exceptions import ValidationError
from django.core.management import call_command
from django.utils import timezone

from scheduling.forms import FiftyFiftyRotationForm, OfficeRotationForm
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
    ShiftTemplate,
    Show,
    WarningSeverity,
    WarningType,
)
from scheduling.services.engine import (
    ApprovedScheduleError,
    SchedulingEngine,
    shift_window_for,
)
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
    # Nine distinct people: the six-plus-two crew, plus the Server Manager.
    assert run.assignments.values("employee_id").distinct().count() == 9

    # A half-six show runs on management's own call times, not on offsets from the
    # curtain: Server 1 is told to come in at three and finish at nine.
    lead = run.assignments.get(shift_template__code="lead-server")
    assert timezone.localtime(lead.start_datetime).time() == time(15, 0)
    assert timezone.localtime(lead.end_datetime).time() == time(21, 0)

    # On-call still carries no paid hours, only standby hours.
    on_call = run.assignments.get(shift_template__code="on-call-server")
    assert timezone.localtime(on_call.start_datetime).time() == time(18, 15)
    assert timezone.localtime(on_call.end_datetime).time() == time(23, 0)
    assert on_call.scheduled_paid_hours == 0
    assert on_call.on_call_hours == Decimal("4.75")

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
def test_spirit_only_employment_confers_no_scheduling_advantage(configured_staff):
    """All servers rank equally - management's decision.

    Olena and Jackie once received a capped boost because Spirit is their only
    employer. Two candidates alike in every other respect must now score identically,
    so the flag can never be what decides a shift.
    """
    from decimal import Decimal as D

    from scheduling.models import ShiftTemplate
    from scheduling.services.allocator import GlobalAllocator, Slot
    from scheduling.services.engine import shift_window_for

    show = make_show(requires_50_50=False, start_time=time(18, 30))
    flagged = Employee.objects.get(display_name="Olena")
    plain = Employee.objects.get(display_name="Molly Rittwage")
    assert flagged.spirit_only_employment is True
    assert plain.spirit_only_employment is False

    template = ShiftTemplate.objects.get(code="lead-server")
    start, end = shift_window_for(show, template)
    slot = Slot(
        show=show, template=template, start=start, end=end,
        hours=D("6.00"), is_on_call=False,
    )
    # Identical targets and carry-in: the flag is the only thing left between them.
    allocator = GlobalAllocator(
        {flagged.id: D("40.00"), plain.id: D("40.00")},
        carry_in_hours={flagged.id: D("10.00"), plain.id: D("10.00")},
    )
    args = dict(remaining_opportunities=5, max_shift_count=3)
    assert allocator.score(flagged, slot, **args) == allocator.score(plain, slot, **args)
    assert not hasattr(allocator, "spirit_only_ids")


@pytest.mark.django_db
def test_the_selection_reason_no_longer_claims_a_priority(configured_staff):
    """The audit trail must not advertise a rule the engine stopped applying."""
    show = make_show(requires_50_50=False)
    make_all_available(configured_staff, show.date)
    run = SchedulingEngine().generate(show.date, show.date, allow_shortages=True)
    reasons = " ".join(run.assignments.values_list("selection_reason", flat=True))
    assert "Spirit-only" not in reasons
    assert reasons  # the run actually assigned somebody


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
def test_rotations_survive_square_renaming_display_names(configured_staff):
    """Square sync rewrites display_name to the full "Yana Pasechniuk" form.

    Both rotations key off hardcoded first-name pairs. Matching those against
    display_name resolved nothing once the sync had run: the office rotation
    raised ImproperlyConfigured and the 50/50 alternation silently returned an
    empty candidate list. Every other fixture here seeds bare first names, so
    nothing caught it until real data hit the engine.
    """
    for first, last in (("Yana", "Pasechniuk"), ("Khrystyna", "Zavadetska"), ("Kate", "Griffin")):
        Employee.objects.filter(display_name=first).update(
            last_name=last, display_name=f"{first} {last}"
        )
    yana = Employee.objects.get(first_name="Yana")
    OfficeRotationConfig.objects.create(seed_date=date(2026, 9, 12), seed_saturday_employee=yana)
    FiftyFiftyRotationConfig.objects.create(seed_employee=yana)

    dates = [date(2026, 9, 12), date(2026, 9, 13), date(2026, 9, 19), date(2026, 9, 20)]
    for show_date in dates:
        make_show(show_date)
    make_all_available(configured_staff, *dates)
    run = SchedulingEngine().generate(dates[0], dates[-1])

    office = list(
        OfficeAssignment.objects.order_by("date").values_list("employee__display_name", flat=True)
    )
    assert office == [
        "Yana Pasechniuk",
        "Khrystyna Zavadetska",
        "Khrystyna Zavadetska",
        "Yana Pasechniuk",
    ]

    fifty = list(
        run.assignments.filter(assignment_type=AssignmentType.FIFTY_FIFTY)
        .order_by("show__date")
        .values_list("employee__first_name", flat=True)
    )
    assert fifty, "the 50/50 rotation produced no assignments at all"
    assert set(fifty) == {"Yana", "Kate"}

    # The configuration page picks the same people through its own querysets and
    # model validators. Filtering those on display_name emptied both dropdowns,
    # so the page rejected every save with "Choose either Yana or Khrystyna" and
    # offered no such choice.
    office_choices = OfficeRotationForm().fields["seed_saturday_employee"].queryset
    fifty_choices = FiftyFiftyRotationForm().fields["seed_employee"].queryset
    assert {e.first_name for e in office_choices} == {"Yana", "Khrystyna"}
    assert {e.first_name for e in fifty_choices} == {"Yana", "Kate"}
    OfficeRotationConfig.objects.first().full_clean()
    FiftyFiftyRotationConfig.objects.first().full_clean()


@pytest.mark.django_db
def test_office_warning_only_when_shift_times_overlap(configured_staff):
    show = make_show(requires_50_50=False)
    make_all_available(configured_staff, show.date)
    yana = Employee.objects.get(display_name="Yana")
    # Ends before the earliest call time of the day (the Server Manager, at 14:00),
    # so it genuinely clashes with nothing.
    OfficeAssignment.objects.create(
        employee=yana,
        date=show.date,
        start_time=time(9),
        end_time=time(13),
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


@pytest.mark.django_db
def test_every_show_gets_a_fifty_fifty_regardless_of_the_legacy_flag(configured_staff):
    """The 50/50 is standard crew, not opt-in.

    Show.requires_50_50 defaults to False and the calendar import never sets it, so
    gating on it left 95 of 96 real shows with no 50/50 rostered at all. The flag is
    deliberately False here: the requirement must appear anyway.
    """
    show = make_show(requires_50_50=False)
    requirements, _ = staffing_requirements_for(show)
    by_role = {r.role_name: r for r in requirements}

    assert by_role["50/50"].confirmed_count == 1
    assert by_role["50/50"].on_call_count == 0


@pytest.mark.django_db
def test_the_standard_crew_for_a_seventy_five_to_hundred_guest_show(configured_staff):
    """3 servers + 1 on-call, 1 bartender + 1 on-call, 1 busser, 1 fifty-fifty."""
    show = make_show(expected_guests=80, requires_50_50=False)
    requirements, _ = staffing_requirements_for(show)
    counts = {r.role_name: (r.confirmed_count, r.on_call_count) for r in requirements}

    assert counts == {
        "Server Manager": (1, 0),
        "Server": (3, 1),
        "Bartender": (1, 1),
        "Busser": (1, 0),
        "50/50": (1, 0),
    }
    # Seven on and two on standby: the six-plus-two crew, plus the Server Manager who
    # comes in mid-afternoon ahead of them.
    assert sum(c for c, _ in counts.values()) == 7
    assert sum(o for _, o in counts.values()) == 2


@pytest.mark.django_db
def test_a_fifty_fifty_is_actually_rostered_on_a_show_that_never_opted_in(configured_staff):
    show = make_show(requires_50_50=False)
    make_all_available(configured_staff, show.date)
    run = SchedulingEngine().generate(show.date, show.date, allow_shortages=True)

    assert run.assignments.filter(assignment_type=AssignmentType.FIFTY_FIFTY).count() == 1


@pytest.mark.django_db
def test_regenerating_a_period_retires_the_previous_run_for_it(configured_staff):
    """One live run per period.

    Regenerating used to leave both attempts in "In progress", each saying Needs
    review, with nothing but the run number to say which was current.
    """
    show = make_show()
    make_all_available(configured_staff, show.date)

    first = SchedulingEngine().generate(show.date, show.date, allow_shortages=True)
    second = SchedulingEngine().generate(show.date, show.date, allow_shortages=True)

    first.refresh_from_db()
    second.refresh_from_db()
    assert first.status == ScheduleRunStatus.SUPERSEDED_SOURCE_DATA
    assert second.status != ScheduleRunStatus.SUPERSEDED_SOURCE_DATA


@pytest.mark.django_db
def test_a_run_already_sent_to_square_is_never_retired_by_a_regeneration(configured_staff):
    """Its shifts are live in Square, so the record explaining them stays current."""
    show = make_show()
    make_all_available(configured_staff, show.date)

    synced = SchedulingEngine().generate(show.date, show.date, allow_shortages=True)
    synced.status = ScheduleRunStatus.SYNCED_TO_SQUARE
    synced.save(update_fields=["status"])

    SchedulingEngine().generate(show.date, show.date, allow_shortages=True)

    synced.refresh_from_db()
    assert synced.status == ScheduleRunStatus.SYNCED_TO_SQUARE


@pytest.mark.django_db
def test_a_run_for_a_different_period_is_left_alone(configured_staff):
    """Only an identical range is retired.

    Superseding anything that merely overlapped would let one regenerated day quietly
    retire the whole month around it.
    """
    day = make_show(date(2026, 9, 12))
    other = make_show(date(2026, 9, 19))
    make_all_available(configured_staff, day.date, other.date)

    month = SchedulingEngine().generate(day.date, other.date, allow_shortages=True)
    SchedulingEngine().generate(day.date, day.date, allow_shortages=True)

    month.refresh_from_db()
    assert month.status != ScheduleRunStatus.SUPERSEDED_SOURCE_DATA


@pytest.fixture
def one_show_run(configured_staff):
    show = make_show()
    make_all_available(configured_staff, show.date)
    return SchedulingEngine().generate(show.date, show.date, allow_shortages=True)


def _free_server(run, show):
    return next(
        e
        for e in Employee.objects.filter(employee_roles__role__name="Server", active=True)
        if not run.assignments.filter(show=show, employee=e).exists()
    )


@pytest.mark.django_db
def test_an_override_can_move_the_shift_window(one_show_run):
    from scheduling.services.workflow import override_assignment

    assignment = one_show_run.assignments.get(shift_template__code="lead-server")
    replacement = _free_server(one_show_run, assignment.show)

    override_assignment(
        assignment,
        replacement,
        "covering a late start",
        start_time=time(18, 15),
        end_time=time(22, 0),
    )

    assignment.refresh_from_db()
    assert timezone.localtime(assignment.start_datetime).time() == time(18, 15)
    assert timezone.localtime(assignment.end_datetime).time() == time(22, 0)
    assert assignment.employee == replacement


@pytest.mark.django_db
def test_moving_the_window_recalculates_the_paid_hours(one_show_run):
    """Leaving the generated hours behind would misreport workload and Square."""
    from scheduling.services.workflow import override_assignment

    assignment = one_show_run.assignments.get(shift_template__code="lead-server")
    replacement = _free_server(one_show_run, assignment.show)

    override_assignment(
        assignment, replacement, "shorter cover", start_time=time(19, 0), end_time=time(22, 0)
    )

    assignment.refresh_from_db()
    assert assignment.scheduled_paid_hours == Decimal("3.00")
    assert assignment.on_call_hours == Decimal("0.00")


@pytest.mark.django_db
def test_an_override_that_leaves_the_times_alone_keeps_the_generated_window(one_show_run):
    from scheduling.services.workflow import override_assignment

    assignment = one_show_run.assignments.get(shift_template__code="lead-server")
    before = (assignment.start_datetime, assignment.end_datetime)
    replacement = _free_server(one_show_run, assignment.show)

    override_assignment(assignment, replacement, "like for like")

    assignment.refresh_from_db()
    assert (assignment.start_datetime, assignment.end_datetime) == before


@pytest.mark.django_db
def test_eligibility_is_checked_against_the_new_window_not_the_generated_one(one_show_run):
    """The check must follow the times being saved.

    Validating the old hours and then writing different ones would wave through
    exactly the case the availability check exists to catch.
    """
    from scheduling.services.workflow import override_assignment

    assignment = one_show_run.assignments.get(shift_template__code="lead-server")
    replacement = _free_server(one_show_run, assignment.show)
    entry = EmployeeAvailability.objects.get(employee=replacement, date=assignment.show.date)
    entry.availability_type = AvailabilityType.AVAILABLE_WINDOW
    entry.start_time = time(17, 0)
    entry.end_time = time(21, 0)
    entry.save()

    with pytest.raises(ValidationError):
        override_assignment(
            assignment, replacement, "runs past their window", end_time=time(23, 30)
        )

    assignment.refresh_from_db()
    assert assignment.employee != replacement


@pytest.mark.django_db
def test_a_shift_ending_after_midnight_is_read_as_the_next_day(one_show_run):
    from scheduling.services.workflow import override_assignment

    assignment = one_show_run.assignments.get(shift_template__code="lead-server")
    replacement = _free_server(one_show_run, assignment.show)

    override_assignment(
        assignment, replacement, "late bar close", start_time=time(21, 0), end_time=time(0, 30)
    )

    assignment.refresh_from_db()
    assert assignment.end_datetime > assignment.start_datetime
    assert assignment.scheduled_paid_hours == Decimal("3.50")


def _shortage_run(configured_staff):
    """A run with a genuine on-call server shortage: only three servers can work."""
    show = make_show()
    make_all_available(configured_staff, show.date)
    keep = list(Employee.objects.filter(employee_roles__role__name="Server", active=True))[:3]
    EmployeeAvailability.objects.filter(date=show.date).exclude(
        employee__in=keep + list(Employee.objects.exclude(employee_roles__role__name="Server"))
    ).update(availability_type=AvailabilityType.UNAVAILABLE)
    run = SchedulingEngine().generate(show.date, show.date, allow_shortages=True)
    return run, show


@pytest.mark.django_db
def test_a_shortage_slot_can_be_filled_by_hand(configured_staff):
    from scheduling.models import ShiftTemplate
    from scheduling.services.workflow import fill_assignment

    run, show = _shortage_run(configured_staff)
    template = ShiftTemplate.objects.get(code="on-call-server")
    if run.assignments.filter(shift_template=template).exists():
        pytest.skip("no shortage produced for this fixture")
    candidate = next(
        e
        for e in Employee.objects.filter(employee_roles__role__name="Server", active=True)
        if not run.assignments.filter(show=show, employee=e).exists()
    )
    EmployeeAvailability.objects.filter(employee=candidate, date=show.date).update(
        availability_type=AvailabilityType.AVAILABLE_ALL_DAY
    )

    fill_assignment(run, show, template, candidate, "manager knows they can come in")

    assignment = run.assignments.get(shift_template=template)
    assert assignment.employee == candidate
    assert assignment.manually_overridden is True


@pytest.mark.django_db
def test_filling_a_shortage_clears_the_hard_error_blocking_approval(configured_staff):
    """Without this the run could never be approved.

    The shortage warning is an ERROR, approval refuses while one is unresolved, and
    the warnings screen that used to clear them by hand has been removed.
    """
    from scheduling.models import ShiftTemplate
    from scheduling.services.workflow import fill_assignment

    run, show = _shortage_run(configured_staff)
    template = ShiftTemplate.objects.get(code="on-call-server")
    if run.assignments.filter(shift_template=template).exists():
        pytest.skip("no shortage produced for this fixture")
    assert run.warnings.filter(
        warning_type=WarningType.ON_CALL_SERVER_SHORTAGE, resolved=False
    ).exists()

    candidate = next(
        e
        for e in Employee.objects.filter(employee_roles__role__name="Server", active=True)
        if not run.assignments.filter(show=show, employee=e).exists()
    )
    EmployeeAvailability.objects.filter(employee=candidate, date=show.date).update(
        availability_type=AvailabilityType.AVAILABLE_ALL_DAY
    )
    fill_assignment(run, show, template, candidate, "covering the gap")

    assert not run.warnings.filter(
        warning_type=WarningType.ON_CALL_SERVER_SHORTAGE, resolved=False
    ).exists()


@pytest.mark.django_db
def test_filling_a_slot_still_enforces_eligibility(configured_staff):
    """Manual does not mean unchecked - a double-booking is still refused."""
    from scheduling.models import ShiftTemplate
    from scheduling.services.workflow import fill_assignment

    run, show = _shortage_run(configured_staff)
    template = ShiftTemplate.objects.get(code="on-call-server")
    if run.assignments.filter(shift_template=template).exists():
        pytest.skip("no shortage produced for this fixture")
    already_working = run.assignments.filter(show=show).first().employee

    with pytest.raises(ValidationError):
        fill_assignment(run, show, template, already_working, "double booking attempt")


@pytest.mark.django_db
def test_a_position_that_is_already_filled_cannot_be_filled_again(configured_staff):
    from scheduling.models import ShiftTemplate
    from scheduling.services.workflow import fill_assignment

    show = make_show()
    make_all_available(configured_staff, show.date)
    run = SchedulingEngine().generate(show.date, show.date, allow_shortages=True)
    template = ShiftTemplate.objects.get(code="lead-server")
    someone = Employee.objects.filter(employee_roles__role__name="Server", active=True).first()

    with pytest.raises(ValidationError, match="already filled"):
        fill_assignment(run, show, template, someone, "should be refused")


@pytest.mark.django_db
def test_an_approved_schedule_can_still_be_corrected(one_show_run):
    """Approval is a decision, not a lock.

    A mistake found after approval is still a mistake, and refusing the edit just
    forces a manager to regenerate the whole period to change one name.
    """
    from scheduling.services.workflow import override_assignment

    one_show_run.status = ScheduleRunStatus.APPROVED
    one_show_run.save(update_fields=["status"])
    assignment = one_show_run.assignments.get(shift_template__code="lead-server")
    replacement = _free_server(one_show_run, assignment.show)

    override_assignment(assignment, replacement, "wrong person approved by mistake")

    assignment.refresh_from_db()
    assert assignment.employee == replacement


@pytest.mark.django_db
def test_a_shortage_can_still_be_filled_after_approval(one_show_run):
    from scheduling.models import ShiftTemplate
    from scheduling.services.workflow import fill_assignment

    one_show_run.status = ScheduleRunStatus.APPROVED
    one_show_run.save(update_fields=["status"])
    show = one_show_run.assignments.first().show
    template = ShiftTemplate.objects.get(code="on-call-server")
    if one_show_run.assignments.filter(shift_template=template).exists():
        one_show_run.assignments.filter(shift_template=template).delete()
    candidate = _free_server(one_show_run, show)

    fill_assignment(one_show_run, show, template, candidate, "correcting after approval")

    assert one_show_run.assignments.filter(shift_template=template).exists()


@pytest.mark.django_db
def test_editing_a_synced_run_says_square_no_longer_matches(one_show_run):
    """The edit is allowed, but the drafts in Square still show the old roster."""
    from scheduling.services.workflow import override_assignment

    one_show_run.status = ScheduleRunStatus.SYNCED_TO_SQUARE
    one_show_run.save(update_fields=["status"])
    assignment = one_show_run.assignments.get(shift_template__code="lead-server")
    replacement = _free_server(one_show_run, assignment.show)

    override_assignment(assignment, replacement, "corrected after sync")

    flagged = one_show_run.warnings.filter(
        warning_type=WarningType.SQUARE_OUT_OF_DATE, resolved=False
    )
    assert flagged.exists()
    assert flagged.first().severity == WarningSeverity.WARNING  # must not block re-approval


@pytest.mark.django_db
def test_editing_an_unsynced_run_raises_no_square_warning(one_show_run):
    from scheduling.services.workflow import override_assignment

    assignment = one_show_run.assignments.get(shift_template__code="lead-server")
    replacement = _free_server(one_show_run, assignment.show)

    override_assignment(assignment, replacement, "ordinary correction")

    assert not one_show_run.warnings.filter(warning_type=WarningType.SQUARE_OUT_OF_DATE).exists()


@pytest.mark.django_db
def test_eligibility_is_still_enforced_on_an_approved_schedule(one_show_run):
    """Unlocking approval must not unlock double-booking."""
    from scheduling.services.workflow import override_assignment

    one_show_run.status = ScheduleRunStatus.APPROVED
    one_show_run.save(update_fields=["status"])
    assignment = one_show_run.assignments.get(shift_template__code="lead-server")
    already_working = (
        one_show_run.assignments.exclude(pk=assignment.pk).first().employee
    )

    with pytest.raises(ValidationError):
        override_assignment(assignment, already_working, "should still be refused")


@pytest.mark.django_db
def test_the_same_person_can_have_their_hours_changed(one_show_run):
    """Editing a shift must not treat that shift as a clash with itself.

    Keeping the person and moving only the times was refused twice over - as an
    overlapping assignment and as a second role for the show - because the check
    counted the very row being rewritten.
    """
    from scheduling.services.workflow import override_assignment

    assignment = one_show_run.assignments.get(shift_template__code="busser")
    same_person = assignment.employee

    override_assignment(
        assignment, same_person, "starting later that night",
        start_time=time(19, 0), end_time=time(22, 30),
    )

    assignment.refresh_from_db()
    assert assignment.employee == same_person
    assert timezone.localtime(assignment.start_datetime).time() == time(19, 0)
    assert timezone.localtime(assignment.end_datetime).time() == time(22, 30)


@pytest.mark.django_db
def test_changing_hours_in_place_still_refuses_a_genuine_clash(one_show_run):
    """Excluding the edited row must not excuse a real double-booking."""
    from scheduling.services.workflow import override_assignment

    busser = one_show_run.assignments.get(shift_template__code="busser")
    lead_server_person = one_show_run.assignments.get(
        shift_template__code="lead-server"
    ).employee

    with pytest.raises(ValidationError):
        override_assignment(busser, lead_server_person, "should still be refused")


@pytest.mark.django_db
def test_hours_changed_in_place_are_recalculated(one_show_run):
    from scheduling.services.workflow import override_assignment

    assignment = one_show_run.assignments.get(shift_template__code="busser")
    override_assignment(
        assignment, assignment.employee, "shorter shift",
        start_time=time(19, 0), end_time=time(22, 0),
    )

    assignment.refresh_from_db()
    assert assignment.scheduled_paid_hours == Decimal("3.00")


# --- management's own call times ------------------------------------------------

STANDARD_EVENING_EXPECTED = {
    "fifty-fifty": (time(17, 45), time(20, 30)),
    "server-manager": (time(14, 0), time(21, 0)),
    "lead-server": (time(15, 0), time(21, 0)),
    "server-2": (time(16, 0), time(21, 30)),
    "server-3": (time(17, 30), time(23, 0)),
    "on-call-server": (time(18, 15), time(23, 0)),
    "busser": (time(18, 45), time(23, 0)),
    "bartender": (time(16, 0), time(23, 0)),
    "on-call-bartender": (time(17, 30), time(22, 30)),
}

DWIGHTS_EXPECTED = {
    "fifty-fifty": (time(17, 30), time(20, 30)),
    "server-manager": (time(14, 0), time(21, 0)),
    "lead-server": (time(15, 0), time(21, 0)),
    "server-2": (time(15, 30), time(21, 30)),
    "server-3": (time(17, 0), time(22, 30)),
    "on-call-server": (time(19, 0), time(22, 30)),
    "busser": (time(19, 15), time(23, 0)),
    "bartender": (time(16, 0), time(22, 30)),
    "on-call-bartender": (time(17, 0), time(22, 30)),
}


def _window(show, code):
    from scheduling.models import ShiftTemplate
    from scheduling.services.engine import shift_window_for

    start, end = shift_window_for(show, ShiftTemplate.objects.get(code=code))
    return timezone.localtime(start).time(), timezone.localtime(end).time()


@pytest.mark.django_db
@pytest.mark.parametrize(("code", "expected"), sorted(STANDARD_EVENING_EXPECTED.items()))
def test_a_half_six_show_uses_managements_call_times(configured_staff, code, expected):
    show = make_show(title="Forever Country", start_time=time(18, 30))
    assert _window(show, code) == expected


@pytest.mark.django_db
@pytest.mark.parametrize(("code", "expected"), sorted(DWIGHTS_EXPECTED.items()))
def test_dwights_wedding_uses_its_own_call_times(configured_staff, code, expected):
    show = make_show(
        title="(It's a Nice Day for) Dwight's Wedding!! - Fall 2026",
        start_time=time(18, 30),
    )
    assert _window(show, code) == expected


@pytest.mark.django_db
def test_dwights_wins_on_title_whichever_time_the_calendar_recorded(configured_staff):
    """Doors are 6:00pm and Act I 6:30pm, so the calendar holds both for Dwight's.

    The title is what reliably says which timetable applies; the start time does not.
    """
    at_doors = make_show(
        date(2026, 9, 12), title="Dwight's Wedding", start_time=time(18, 0)
    )
    at_curtain = make_show(
        date(2026, 9, 19), title="Dwight's Wedding", start_time=time(18, 30)
    )
    expected = (time(19, 15), time(23, 0))
    assert _window(at_doors, "busser") == expected
    assert _window(at_curtain, "busser") == expected


@pytest.mark.django_db
def test_an_unusual_show_still_derives_its_window_from_its_own_times(configured_staff):
    """A matinee is on neither list, and must not be forced onto an evening clock."""
    show = make_show(title="Ugly Stick Workshop", start_time=time(13, 0), end_time=time(15, 0))
    start, end = _window(show, "lead-server")
    assert start < time(13, 0)  # in before doors, derived - not the 15:00 evening call


@pytest.mark.django_db
def test_extra_servers_and_bartenders_share_the_crews_clock(configured_staff):
    show = make_show(title="Forever Country", start_time=time(18, 30))
    assert _window(show, "server-4") == _window(show, "server-3")
    assert _window(show, "bartender-2") == _window(show, "bartender")


@pytest.mark.django_db
def test_every_show_asks_for_a_server_manager(configured_staff):
    requirements, _ = staffing_requirements_for(make_show())
    counts = {r.role_name: (r.confirmed_count, r.on_call_count) for r in requirements}
    assert counts["Server Manager"] == (1, 0)


@pytest.mark.django_db
def test_the_manager_is_schedulable_for_her_own_role_but_nothing_else(configured_staff):
    """She is excluded from the ordinary rota; the exemption is scoped to this role."""
    from scheduling.models import EmployeeRole, Role, ScheduleRun, ShiftTemplate
    from scheduling.services.eligibility import EligibilityService
    from scheduling.services.engine import shift_window_for

    # Seeded by seed_spirit_staff; mark her excluded the way the real roster does.
    manager = Employee.objects.get(display_name="Deborah Sweetapple")
    manager.excluded_from_automatic_scheduling = True
    manager.save(update_fields=["excluded_from_automatic_scheduling"])
    EmployeeRole.objects.update_or_create(
        employee=manager,
        role=Role.objects.get(name="Server"),
        defaults={"active": True, "capability_level": 5},
    )
    show = make_show(start_time=time(18, 30))
    make_all_available([manager], show.date)
    # A bare run, not a generated one: generating would assign her to Server Manager
    # and the check below would then see her own assignment as the clash.
    run = ScheduleRun.objects.create(
        start_date=show.date, end_date=show.date, status=ScheduleRunStatus.DRAFT
    )

    def eligible_for(code, role_name):
        template = ShiftTemplate.objects.get(code=code)
        start, end = shift_window_for(show, template)
        return EligibilityService().evaluate(
            manager, Role.objects.get(name=role_name), show, template, run, start, end
        ).eligible

    assert eligible_for("server-manager", "Server Manager") is True
    assert eligible_for("lead-server", "Server") is False


@pytest.mark.django_db
def test_an_absent_manager_is_reported_as_a_shortage_not_quietly_skipped(configured_staff):
    from scheduling.models import EmployeeTimeOff, TimeOffStatus

    manager = Employee.objects.get(display_name="Deborah Sweetapple")
    show = make_show(start_time=time(18, 30))
    make_all_available(configured_staff, show.date)
    EmployeeTimeOff.objects.create(
        employee=manager, start_date=show.date, end_date=show.date,
        status=TimeOffStatus.APPROVED, reason="Out of province",
    )

    run = SchedulingEngine().generate(show.date, show.date, allow_shortages=True)

    assert not run.assignments.filter(shift_template__code="server-manager").exists()
    assert run.warnings.filter(warning_type=WarningType.SERVER_MANAGER_SHORTAGE).exists()


@pytest.mark.django_db
def test_the_manager_is_scheduled_although_square_holds_no_availability_for_her(
    configured_staff,
):
    """Managers do not file weekly availability; the blank must not read as a refusal.

    Every other role treats an unknown as a hard no, and must keep doing so - the
    exemption is for the one position whose rule is "in for every show unless she has
    booked time off".
    """
    manager = Employee.objects.get(display_name="Deborah Sweetapple")
    show = make_show(start_time=time(18, 30))
    make_all_available([e for e in configured_staff if e != manager], show.date)
    assert not EmployeeAvailability.objects.filter(employee=manager, date=show.date).exists()

    run = SchedulingEngine().generate(show.date, show.date, allow_shortages=True)

    assignment = run.assignments.get(shift_template__code="server-manager")
    assert assignment.employee == manager
    assert timezone.localtime(assignment.start_datetime).time() == time(14, 0)
    assert not run.warnings.filter(warning_type=WarningType.SERVER_MANAGER_SHORTAGE).exists()


@pytest.mark.django_db
def test_availability_square_actually_holds_still_counts_against_the_manager(
    configured_staff,
):
    """The exemption covers a missing record, not a recorded one."""
    from scheduling.models import AvailabilityType, Role, ScheduleRun, ShiftTemplate
    from scheduling.services.eligibility import EligibilityService
    from scheduling.services.engine import shift_window_for

    manager = Employee.objects.get(display_name="Deborah Sweetapple")
    show = make_show(start_time=time(18, 30))
    EmployeeAvailability.objects.create(
        employee=manager,
        date=show.date,
        availability_type=AvailabilityType.AVAILABLE_WINDOW,
        start_time=time(17, 0),
        end_time=time(21, 0),
    )
    run = ScheduleRun.objects.create(
        start_date=show.date, end_date=show.date, status=ScheduleRunStatus.DRAFT
    )
    template = ShiftTemplate.objects.get(code="server-manager")
    start, end = shift_window_for(show, template)
    result = EligibilityService().evaluate(
        manager, Role.objects.get(name="Server Manager"), show, template, run, start, end
    )

    # She is called at 14:00 and Square says she is free from 17:00 - that is a real
    # conflict, recorded by a person, and it stands.
    assert result.eligible is False
    assert any("not fully covered" in reason for reason in result.reasons)


# --- fitting a shift to what someone can actually work ---------------------------


@pytest.mark.django_db
@pytest.mark.parametrize(
    ("avail_start", "avail_end", "code", "expected"),
    [
        # Management's own examples: free 16:00-20:30 takes the front of Server 3;
        # free 17:30-21:30 covers the whole 50/50 with nothing to trim.
        (time(16, 0), time(20, 30), "server-3", (time(17, 30), time(20, 30))),
        # 50/50 runs 17:45-20:30 on an ordinary evening, which 17:30-21:30 covers
        # outright - so nothing is trimmed and she works the published window.
        (time(17, 30), time(21, 30), "fifty-fifty", (time(17, 45), time(20, 30))),
    ],
)
def test_a_shift_is_trimmed_to_the_hours_someone_is_free(
    configured_staff, avail_start, avail_end, code, expected
):
    """Narrow availability earns the part of the shift it covers, not a refusal."""
    from scheduling.models import ShiftTemplate
    from scheduling.services.availability import LocalAvailabilityProvider
    from scheduling.services.engine import shift_window_for

    show = make_show(start_time=time(18, 30))
    kate = Employee.objects.get(display_name="Kate")
    EmployeeAvailability.objects.update_or_create(
        employee=kate,
        date=show.date,
        defaults={
            "availability_type": AvailabilityType.AVAILABLE_WINDOW,
            "start_time": avail_start,
            "end_time": avail_end,
            "available": True,
        },
    )
    start, end = shift_window_for(show, ShiftTemplate.objects.get(code=code))
    window = LocalAvailabilityProvider().fit(kate, show.date, start.time(), end.time())

    assert window is not None
    assert (window.start_time, window.end_time) == expected


@pytest.mark.django_db
def test_an_overlap_under_three_hours_is_not_worth_the_trip(configured_staff):
    from scheduling.services.availability import MIN_FITTED_SHIFT_HOURS, LocalAvailabilityProvider

    assert MIN_FITTED_SHIFT_HOURS == 3.0
    show = make_show(start_time=time(18, 30))
    kate = Employee.objects.get(display_name="Kate")
    EmployeeAvailability.objects.update_or_create(
        employee=kate,
        date=show.date,
        defaults={
            "availability_type": AvailabilityType.AVAILABLE_WINDOW,
            "start_time": time(19, 30),
            "end_time": time(21, 0),  # 1.5h of a 15:00-21:00 lead shift
            "available": True,
        },
    )
    assert LocalAvailabilityProvider().fit(kate, show.date, time(15, 0), time(21, 0)) is None


@pytest.mark.django_db
def test_a_part_covered_shift_says_which_hours_nobody_is_on(configured_staff):
    """Filling a slot partly is better than leaving it empty, but it is not covering it."""
    from scheduling.services.engine import shift_window_for

    show = make_show(start_time=time(18, 30))
    make_all_available(configured_staff, show.date)
    kate = Employee.objects.get(display_name="Kate")
    # She also holds the 50/50 role, which is settled first and would take her out of
    # the running for the lead seat; this test is about the floor, so keep her on it.
    EmployeeRole.objects.filter(employee=kate, role__name="50/50").update(active=False)
    # Kate is the only server free at all, so the lead shift is hers or nobody's -
    # otherwise a fully-available colleague wins it and this proves nothing.
    others = [e for e in configured_staff if e != kate]
    EmployeeAvailability.objects.filter(employee__in=others, date=show.date).update(
        availability_type=AvailabilityType.UNAVAILABLE, available=False
    )
    EmployeeAvailability.objects.filter(employee=kate, date=show.date).update(
        availability_type=AvailabilityType.AVAILABLE_WINDOW,
        start_time=time(16, 0),
        end_time=time(20, 30),
        available=True,
    )
    run = SchedulingEngine().generate(show.date, show.date, allow_shortages=True)

    lead = run.assignments.filter(shift_template__code="lead-server").first()
    assert lead is not None and lead.employee == kate
    # She works her own hours, not the call window's.
    assert timezone.localtime(lead.start_datetime).time() == time(16, 0)
    assert timezone.localtime(lead.end_datetime).time() == time(20, 30)
    assert (lead.start_datetime, lead.end_datetime) != shift_window_for(show, lead.shift_template)

    # And the hours nobody is on are spelled out rather than passing silently.
    gap = run.warnings.filter(
        warning_type=WarningType.PARTIAL_SHIFT_COVERAGE, message__contains="Kate"
    ).first()
    assert gap is not None
    assert "15:00-16:00" in gap.message and "20:30-21:00" in gap.message


@pytest.mark.django_db
def test_separate_runs_do_not_hand_the_same_person_the_same_slot(configured_staff):
    """Rotation across runs, which is what makes the roster stop repeating itself.

    Each run used to start the whole roster at zero hours, because carry-in came from a
    field nothing writes. The same tie-break order then won every time, so generating
    one date at a time produced the same names in the same slots night after night.
    """
    dates = [date(2026, 9, 12), date(2026, 9, 19), date(2026, 9, 26)]
    bussers = []
    for show_date in dates:
        show = make_show(show_date, start_time=time(18, 30))
        make_all_available(configured_staff, show.date)
        run = SchedulingEngine().generate(show.date, show.date, allow_shortages=True)
        busser = run.assignments.filter(shift_template__code="busser").first()
        if busser:
            bussers.append(busser.employee.display_name)

    assert len(bussers) == 3
    assert len(set(bussers)) > 1, f"the same busser every night: {bussers}"


@pytest.mark.django_db
def test_call_times_are_never_trimmed_while_somebody_can_work_them(configured_staff):
    """The published call times are the rule; fitting is only ever a fallback.

    Regression: a weighted preference for full coverage let a candidate with narrow
    availability outbid a fully available one, so shifts came out shorter than the
    timetable management had actually set. Standby was also settled before the floor,
    which spent the broadly-available people on on-call and left only partial cover for
    the confirmed seats. Both are pinned here.
    """
    show = make_show(start_time=time(18, 30))
    make_all_available(configured_staff, show.date)
    # One person can only do the middle of the evening. Everyone else is free all day.
    kate = Employee.objects.get(display_name="Kate")
    EmployeeAvailability.objects.filter(employee=kate, date=show.date).update(
        availability_type=AvailabilityType.AVAILABLE_WINDOW,
        start_time=time(17, 0),
        end_time=time(20, 30),
        available=True,
    )
    run = SchedulingEngine().generate(show.date, show.date, allow_shortages=True)

    for assignment in run.assignments.select_related("shift_template"):
        expected_start, expected_end = shift_window_for(show, assignment.shift_template)
        assert (assignment.start_datetime, assignment.end_datetime) == (
            expected_start,
            expected_end,
        ), f"{assignment.shift_template.name} was trimmed with full cover available"
    assert not run.warnings.filter(warning_type=WarningType.PARTIAL_SHIFT_COVERAGE).exists()


@pytest.mark.django_db
def test_the_floor_is_staffed_before_standby(configured_staff):
    """A confirmed seat gets first call on anyone who can work it; on-call takes what is left."""
    from scheduling.services.allocator import Slot, slot_priority

    show = make_show(start_time=time(18, 30))
    templates = {t.code: t for t in ShiftTemplate.objects.all()}

    def slot_for(code, candidate_count):
        template = templates[code]
        start, end = shift_window_for(show, template)
        return Slot(
            show=show, template=template, start=start, end=end,
            hours=Decimal("6.00"),
            is_on_call=template.assignment_type == AssignmentType.ON_CALL,
            candidates=[None] * candidate_count,
        )

    # Standby with a tiny pool still waits for a confirmed seat with a big one.
    standby = slot_for("on-call-server", 1)
    floor = slot_for("server-3", 9)
    assert slot_priority(floor) < slot_priority(standby)


@pytest.mark.django_db
def test_everyone_available_reaches_a_fair_month_of_confirmed_shifts(configured_staff):
    """Nobody available should be left on standby all month.

    Balancing hours alone kept handing the confirmed seats to whoever's availability
    covered the widest windows, so a person free only for the middle of the evening
    collected on-call after on-call and almost no real shifts.
    """
    from scheduling.services.allocator import MONTHLY_CONFIRMED_SHIFT_TARGET

    show_dates = [date(2026, 9, 5) + timedelta(days=7 * i) for i in range(8)]
    confirmed = {}
    for show_date in show_dates:
        show = make_show(show_date, start_time=time(18, 30))
        make_all_available(configured_staff, show.date)
        # One person can only ever work the middle of the evening.
        molly = Employee.objects.get(display_name="Molly Rittwage")
        EmployeeAvailability.objects.filter(employee=molly, date=show.date).update(
            availability_type=AvailabilityType.AVAILABLE_WINDOW,
            start_time=time(17, 30),
            end_time=time(21, 30),
            available=True,
        )
        run = SchedulingEngine().generate(show.date, show.date, allow_shortages=True)
        for assignment in run.assignments.filter(assignment_type=AssignmentType.CONFIRMED):
            name = assignment.employee.display_name
            confirmed[name] = confirmed.get(name, 0) + 1

    assert confirmed.get("Molly Rittwage", 0) >= MONTHLY_CONFIRMED_SHIFT_TARGET, (
        f"narrow availability still means almost no confirmed work: {confirmed}"
    )


@pytest.mark.django_db
def test_a_position_does_not_stay_with_one_person_show_after_show(configured_staff):
    """Whoever starts at three should change from night to night."""
    starters = []
    for offset in range(6):
        show = make_show(date(2026, 9, 5) + timedelta(days=7 * offset), start_time=time(18, 30))
        make_all_available(configured_staff, show.date)
        run = SchedulingEngine().generate(show.date, show.date, allow_shortages=True)
        lead = run.assignments.filter(shift_template__code="lead-server").first()
        if lead:
            starters.append(lead.employee.display_name)

    assert len(starters) >= 5
    assert len(set(starters)) >= 3, f"the same few people always start: {starters}"
