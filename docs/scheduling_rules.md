# Scheduling rules and future direction

Phase 2 implements the core deterministic eligibility, staffing, rotation, fairness, warning, review, override, and local-approval rules below. Items explicitly described as future direction are not yet implemented. No phase currently publishes Square schedules or writes to Square Production.

## Decision order

1. Confirm that a show exists and read its expected guest count.
2. Calculate role demand from configurable staffing rules.
3. Filter out employees who are unavailable, unqualified, legally restricted, in conflict, or over work-hour limits.
4. Reserve enough bartender-capable employees before assigning cross-trained people to ordinary server work.
5. Apply the minimum qualification and reliability gate for each role.
6. Rank eligible employees by workload and opportunity fairness.
7. Build separate confirmed and on-call assignments.
8. Report shortages and explain each recommendation.
9. Require management review before sending drafts to Square.
10. Keep Square publication as a separate manual management action.

Qualification is checked before fairness. Fairness must never make an employee eligible for work they cannot legally or operationally perform.

## Demand and capacity direction

- The 100-guest starting point is 3 confirmed servers, 1 on-call server, 1 confirmed bartender, 1 on-call bartender, and 1 busser, plus 50/50 when required.
- The 100-guest case is a baseline, not a permanent capacity assumption.
- Venue capacity is 175 guests.
- Future rules must calculate staffing dynamically from expected guest count and must surface shortages instead of silently understaffing.
- Source notes describe a one-server-per-25-guests direction and bartender thresholds that increase around 81 and 140 guests. Final thresholds and on-call quantities require management approval before implementation.
- Schedules are normally prepared two weeks in advance.

## Eligibility and role protection

- Availability must be checked against the complete shift window before assignment.
- Role qualification and capability level must be checked before fairness ranking.
- Bartender-capable employees are a scarce-skill pool. Reserve required bar coverage before using cross-trained employees as ordinary servers.
- One employee cannot perform server and 50/50 work simultaneously for the same show.
- Legal restrictions, including alcohol-service eligibility, must be represented explicitly in a later model.
- Conflicting confirmed, on-call, office, or other assignments must be detected.

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

## Employee-specific direction

- Olena: Spirit is her only employer. Prioritize meaningful confirmed paid hours when possible.
- Jackie Pynn: Spirit is her only employer. Prioritize meaningful confirmed paid hours while preserving bartender coverage because she is cross-trained.
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
