"""Global scarcity-ordered, deficit-driven slot allocation.

Replaces per-show greedy assignment. The greedy approach walked shows in date
order and filled each slot with whoever ranked best at that instant, which
starved later shows of flexible staff and let high-availability employees run
far past their target hours while restricted-availability staff sat near zero.

This allocator instead plans the whole period at once:

  Stage A  Build the full (employee x slot) eligibility graph for every slot in
           the run, so the allocator knows every option before committing any.
  Stage B  Repeatedly take the *most constrained* unfilled slot -- fewest
           eligible candidates remaining -- and give it to the candidate who is
           furthest below their fair share right now. Assigning scarce slots
           first stops a slot's only possible candidate from being spent
           elsewhere; picking by live deficit keeps hours converging on target
           instead of accumulating with whoever happens to be free.

Both stages sit strictly *after* hard eligibility, so nothing here can schedule
someone unavailable, unqualified, or barred from a role.
"""

from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import Decimal

from scheduling.models import AssignmentType, Employee, ShiftTemplate, Show

# Deficit-score weights. Hours deficit dominates: it is the term that actually
# equalises the roster. The rest shape choices between similarly-starved people.
WEIGHT_HOURS_DEFICIT = 0.50
WEIGHT_OPPORTUNITY_SCARCITY = 0.20
WEIGHT_SHIFT_COUNT_DEFICIT = 0.15
WEIGHT_CARRY_IN_HISTORY = 0.12
WEIGHT_ROLE_PREFERENCE = 0.08
WEIGHT_WEEKEND_BALANCE = 0.05

PENALTY_CONSECUTIVE_NIGHT = 0.12
PENALTY_OVER_TARGET = 0.60

# On-call restricts an evening without paying, so it is tracked against its own
# budget rather than being mixed into paid-hours fairness.
ON_CALL_TARGET_SHARE = 0.35


@dataclass
class Slot:
    show: Show
    template: ShiftTemplate
    start: object
    end: object
    hours: Decimal
    is_on_call: bool
    candidates: list[Employee] = field(default_factory=list)

    @property
    def key(self) -> tuple[int, int]:
        return (self.show.id, self.template.id)


@dataclass
class RunningTotals:
    """Live per-employee tallies, updated as the allocator commits assignments."""

    paid_hours: Decimal = Decimal("0.00")
    on_call_hours: Decimal = Decimal("0.00")
    shift_count: int = 0
    weekend_count: int = 0
    dates: set[date] = field(default_factory=set)


class GlobalAllocator:
    def __init__(
        self,
        targets: dict[int, Decimal],
        on_call_target_hours: dict[int, Decimal] | None = None,
        carry_in_hours: dict[int, Decimal] | None = None,
        preferred_role_ids: dict[int, int] | None = None,
    ):
        self.targets = targets
        self.on_call_targets = on_call_target_hours or {}
        self.totals: dict[int, RunningTotals] = {}
        self.carry_in_hours = carry_in_hours or {}
        # Where a cross-trained employee would rather work. Jackie Pynn holds both bar
        # and floor qualifications but wants the floor, and the published rosters bear
        # that out. A nudge, never a gate: bar cover is still reserved first, so she
        # will still take the bar when nobody else can.
        self.preferred_role_ids = preferred_role_ids or {}
        self._max_carry_in = float(max(self.carry_in_hours.values(), default=Decimal("0.00")))

    def carry_in_fairness(self, employee_id: int) -> float:
        """Prior-period hours as a 0.0-1.0 score, highest for whoever worked least.

        Within-run deficit alone resets everyone to equal at the start of each run, so
        without this term someone who worked heavily last period competes on level footing
        with someone who barely worked at all. It is deliberately a tilt, not a veto: a
        heavy carry-in lowers priority but never makes anyone ineligible, so it cannot
        override availability, qualification, or scarce-skill protection.
        """
        if self._max_carry_in <= 0:
            return 0.0
        worked = float(self.carry_in_hours.get(employee_id, Decimal("0.00")))
        return 1.0 - min(worked / self._max_carry_in, 1.0)

    def totals_for(self, employee_id: int) -> RunningTotals:
        if employee_id not in self.totals:
            self.totals[employee_id] = RunningTotals()
        return self.totals[employee_id]

    def hours_deficit_ratio(self, employee_id: int) -> float:
        """How far below target this employee currently sits, as 0.0-1.0.

        Returns a negative value once past target so the over-target penalty can
        push them behind anyone still short of theirs.
        """
        target = float(self.targets.get(employee_id, Decimal("40.00")))
        if target <= 0:
            return 0.0
        assigned = float(self.totals_for(employee_id).paid_hours)
        return (target - assigned) / target

    def on_call_deficit_ratio(self, employee_id: int) -> float:
        default = self.targets.get(employee_id, Decimal("40.00")) * Decimal(
            str(ON_CALL_TARGET_SHARE)
        )
        target = float(self.on_call_targets.get(employee_id, default))
        if target <= 0:
            return 0.0
        assigned = float(self.totals_for(employee_id).on_call_hours)
        return (target - assigned) / target

    def score(
        self,
        employee: Employee,
        slot: Slot,
        remaining_opportunities: int,
        max_shift_count: int,
    ) -> float:
        totals = self.totals_for(employee.id)

        if slot.is_on_call:
            deficit = self.on_call_deficit_ratio(employee.id)
        else:
            deficit = self.hours_deficit_ratio(employee.id)

        score = WEIGHT_HOURS_DEFICIT * max(deficit, 0.0)
        if deficit < 0:
            score -= PENALTY_OVER_TARGET * min(abs(deficit), 1.0)

        # Someone with few remaining eligible slots should take the ones they can
        # reach; someone available most nights can afford to wait.
        if remaining_opportunities > 0:
            score += WEIGHT_OPPORTUNITY_SCARCITY * (1.0 / remaining_opportunities)

        if max_shift_count > 0:
            shift_deficit = (max_shift_count - totals.shift_count) / max_shift_count
            score += WEIGHT_SHIFT_COUNT_DEFICIT * max(shift_deficit, 0.0)

        # Carry hours worked in previous periods forward, so a run does not start
        # everyone from zero and hand equal footing to someone who just worked a heavy
        # stretch.
        score += WEIGHT_CARRY_IN_HISTORY * self.carry_in_fairness(employee.id)

        # There is deliberately no term for who Spirit is an employee's only job.
        # Olena and Jackie used to receive a capped boost here on that basis;
        # management's decision is that every server competes on the same footing, so
        # the only things separating them are hours worked, scarcity and rest.

        preferred = self.preferred_role_ids.get(employee.id)
        if preferred is not None and preferred == slot.template.role_id:
            score += WEIGHT_ROLE_PREFERENCE

        is_weekend = slot.show.date.weekday() >= 4
        if is_weekend and totals.shift_count > 0:
            weekend_share = totals.weekend_count / max(totals.shift_count, 1)
            score += WEIGHT_WEEKEND_BALANCE * (1.0 - weekend_share)

        previous_night = slot.show.date - timedelta(days=1)
        if previous_night in totals.dates:
            score -= PENALTY_CONSECUTIVE_NIGHT

        return score

    def commit(self, employee: Employee, slot: Slot) -> None:
        totals = self.totals_for(employee.id)
        if slot.is_on_call:
            totals.on_call_hours += slot.hours
        else:
            totals.paid_hours += slot.hours
        totals.shift_count += 1
        if slot.show.date.weekday() >= 4:
            totals.weekend_count += 1
        totals.dates.add(slot.show.date)


def slot_priority(slot: Slot) -> tuple:
    """Most-constrained-first: fewest eligible candidates wins.

    Scarce-skill slots are settled ahead of ordinary ones at equal candidate
    count so bar and 50/50 coverage is never spent on a general server seat.
    """
    role_rank = {"50/50": 0, "Bartender": 1, "Server": 2, "Busser": 3}.get(
        slot.template.role.name, 9
    )
    on_call_rank = 1 if slot.template.assignment_type == AssignmentType.ON_CALL else 0
    return (len(slot.candidates), on_call_rank, role_rank, slot.show.date, slot.template.id)
