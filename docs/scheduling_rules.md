# Scheduling rules and future direction

Phase 2 implements the core deterministic eligibility, staffing, rotation, fairness, warning, review, override, and local-approval rules below. Items explicitly described as future direction are not yet implemented. No phase currently publishes Square schedules or writes to Square Production.

## Decision order

1. Confirm that a show exists and read its expected guest count. If that count is below
   75 the show does not run: flag it for cancellation or guest transfer and stop here.
2. Calculate role demand from the staffing ladder for that guest count.
3. Filter out employees who are unavailable, unqualified, legally restricted, in conflict, or over work-hour limits.
4. Reserve enough bartender-capable employees before assigning cross-trained people to ordinary server work.
5. Apply the minimum qualification and reliability gate for each role.
6. Allocate globally, not per-show: build the full eligibility graph for the period, settle the most-constrained slot first, and select by live hours-deficit. See `docs/ranking_logic.md` for why per-show greedy ranking was replaced.
7. Build separate confirmed and on-call assignments.
8. Report shortages and explain each recommendation.
9. Require management review before sending drafts to Square.
10. Keep Square publication as a separate manual management action.

Qualification is checked before fairness. Fairness must never make an employee eligible for work they cannot legally or operationally perform.

## Demand and capacity: the operating reality

Schedules are published **two weeks in advance**. Management deliberately schedules to the
staffing level a show needs rather than to the level it might turn out to need, because
telling rostered staff that a show is cancelled is easy, and finding staff who can work a
specific date and time on short notice is not. Read every rule below in that light: the
ladder errs toward booking crew, and shortages are surfaced loudly rather than absorbed
silently.

### Show viability

- **A show below 75 guests does not run.** Management either cancels it or moves those
  guests onto another date.
- **75-80 guests is the working buffer** management plans against. A show with no guest
  count entered is planned at 80 (`DEFAULT_EXPECTED_GUESTS`), not at an optimistic round
  number: planning higher over-hires, planning lower implies a show that will not run.
- Venue capacity is 175 guests, reached mainly at Christmas.

### Staffing ladder

Servers scale with the room at **one server per 25 guests**. Bartenders and bussers are
per-show roles that step up as the ladder climbs. Because no show runs below 75 guests,
**three confirmed servers is a hard floor on every show that runs at all** — enforced in
code, and a breach escalates the whole run to management review.

| Planning guests | Confirmed servers | On-call servers | Confirmed bartenders | On-call bartenders | Bussers |
|---|---|---|---|---|---|
| under 75 | *show cancelled or guests moved — no staffing* |
| 75-99 | 3 | 1 | 1 | 1 | 1 |
| 100-124 | 4 | 1 | 2 | 1 | 1 |
| 125-149 | 5 | 2 | 2 | 2 | 2 |
| 150-175 | 6 | 3 | 3 | 2 | 2 |

Plus one 50/50 employee on every show that requires it.

- Confirmed servers are `floor(guests / 25)`, clamped to a floor of 3 and a ceiling of 6.
  At full house management caps confirmed servers at 6 and absorbs the remainder through
  on-call rather than confirming a seventh.
- The 75-99 and 150-175 bands are management-stated values. The two middle bands are
  derived from the same ratio and remain open to adjustment.
- **Busser counts of 2 in the 125+ bands are inferred, not management-stated**, and need
  sign-off. Every other number in the table came directly from management.
- Full house is also a **hiring** event, not only a scheduling one: the 150-175 band needs
  17 filled positions against a current pool of 11 staff with availability on file.
- Surge positions (`server-4` through `server-6`, `on-call-server-2/3`, `bartender-2/3`,
  `on-call-bartender-2`, `busser-2`) exist as templates and stay dormant until the guest
  count reaches their band.

## Eligibility and role protection

- Availability must be checked against the complete shift window before assignment.
- Role qualification and capability level must be checked before fairness ranking.
- Bartender-capable employees are a scarce-skill pool. Reserve required bar coverage before using cross-trained employees as ordinary servers.
- One employee cannot perform server and 50/50 work simultaneously for the same show.
- **Bussers are the under-19 role and are hard-blocked from Server, Bartender, and 50/50 assignments** — they cannot serve alcohol. This is enforced in eligibility, not just in role data entry.
- **Lead Server requires Capability Level 4 or 5**, enforced as a hard eligibility gate (not only a tie-break).
- **Workshop-titled calendar events (e.g. Ugly Stick Workshop) receive no staffing** — they are a class, not a dinner-theatre show.
- Conflicting confirmed, on-call, office, or other assignments must be detected.
- Each role's shift window is derived from that show's own doors/act/dinner/wrap timing (read per-show from the live calendar), not a single fixed clock time shared by every show.

## Fairness direction

- Balance recent actual worked hours, not only scheduled hours.
- Balance confirmed shift opportunities.
- Track offers and declined opportunities separately from worked shifts.
- Track on-call burden separately, including on-call assignments that were never activated.
- Reliability and execution performance can be tie-breakers after eligibility and opportunity fairness.
- Level 5 expands eligibility and coverage options; it must not automatically put someone first in every queue.
- Every recommendation and deprioritization must be explainable to management.
- Manager overrides require a recorded reason and must still satisfy every hard constraint.

Illustrative source weighting is 35% opportunity fairness, 25% recent workload fairness, 20% reliability, 15% execution performance, and 5% additional qualification. These weights are not approved implementation constants.

- Per pay-period confirmed-hours targets: Server 40–50 hrs, Busser 20–24 hrs, Bartender (incl. cross-trained Bartender+Server) 40–60 hrs. Applied as a capped ranking nudge via each employee's `target_hours` preference — never a hard override of availability, qualification, or scarce-skill protection.

## Employee-specific direction

- Olena: Spirit is her only employer. Prioritize meaningful confirmed paid hours when possible. The boost is capped and scales with her remaining deficit, so it decays to zero at target; she is also eligible for on-call.
- Jackie Pynn: Spirit is her only employer. Prioritize meaningful confirmed paid hours while preserving bartender coverage because she is cross-trained. Same capped, deficit-decaying boost as Olena; also on-call eligible.
- Yana and Kate: both are Level 3 server-capable and can perform 50/50. Rotate 50/50 and service assignments fairly, and never assign both roles simultaneously.
- Yana and Khrystyna: alternate weekend office days. Office work does not automatically prevent an evening theatre shift, but actual availability and rest constraints still apply.
- Deborah Sweetapple, Service Manager, is excluded from automatic staffing.
- John Harris, Bar Manager, is excluded from automatic staffing.

## Future data architecture

Later approved phases can add:

- `Show`
- `ExpectedGuestCount`
- `EmployeeAvailability`
- `ShiftTemplate`
- `ScheduleRun`
- `ScheduleAssignment`
- `OnCallAssignment`
- `OfficeAssignment`
- `SquareSyncRun`
- `SquareShiftMapping`
- `AuditLog`

The workflow remains: generate a local draft roster, review and override locally, create draft scheduled shifts in Square, review them in Square, and let management publish manually. Actual Square timecards then feed the next fairness cycle.
