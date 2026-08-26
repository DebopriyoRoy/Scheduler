# Spirit of Newfoundland - Fair Staff Scheduling & Ranking Logic

## Overview
This document translates the agreed Spirit scheduling logic into a 4-stage decision ranking model inside the scheduling engine. Capability determines what an employee can do; fairness determines whose turn it is. Reliability, performance, and role fit break close ties—they do not dominate the schedule.

---

## 4-Stage Scheduling Architecture

```
SHOW + GUEST COUNT
       ↓
STAGE 1: STAFFING DEMAND CALCULATION
       ↓
STAGE 2: HARD ELIGIBILITY FILTERING
       ↓
STAGE 3: SCARCE SKILL PROTECTION
       ↓
STAGE 4: FAIRNESS RANKING & SELECTION
       ↓
CONFIRMED ASSIGNMENTS & SEPARATE ON-CALL RANKING
```

### Stage 1: Staffing Demand Calculation
Calculated independently from employee ranking, from the show's planning guest count.

A show below **75 guests does not run** — management cancels it or moves the guests — so
Stage 1 stops there and flags the decision rather than staffing a night that will not
happen. Above that floor, servers scale at **one per 25 guests** and the ladder runs:

| Planning guests | Svr | On-call Svr | Bar | On-call Bar | Busser |
|---|---|---|---|---|---|
| 75-99 | 3 | 1 | 1 | 1 | 1 |
| 100-124 | 4 | 1 | 2 | 1 | 1 |
| 125-149 | 5 | 2 | 2 | 2 | 2 |
| 150-175 | 6 | 3 | 3 | 2 | 2 |

Plus one 50/50 employee when the show requires it. A show with no guest count entered is
planned at the **80-guest buffer**, which is what management actually plans against, not
an optimistic round number.

**Three confirmed servers is a hard floor**, not a target: it follows from the one-per-25
ratio at the 75-guest minimum, so any show that runs at all carries at least three.
`_enforce_minimum_server_floor()` re-checks this after allocation and raises an
ERROR-severity warning — escalating the entire run to management review — if a show lands
under it. That is deliberate: the schedule publishes two weeks ahead precisely so a
shortage surfaces while there is still time to act on it.

See `docs/scheduling_rules.md` for the full ladder including which numbers are
management-stated and which are derived.

### Stage 2: Hard Eligibility Filter
Evaluates mandatory operational constraints before any candidate receives a fairness score:
- Active roster employee (managers Deborah Sweetapple & John Harris are excluded)
- Authoritative role qualification
- Continuous full-shift availability (UNKNOWN availability is ineligible)
- No office time overlap
- No existing Square shift or theatre shift conflict
- Legal and role restrictions:
  - **Bussers are under 19 and cannot perform alcohol-service or server-facing work.** A
    Busser is ineligible for any Server, Bartender, or 50/50 assignment even if a data-entry
    error ever grants them that role — this is a hard block, not a ranking penalty.
  - **Lead Server requires Capability Level 4 or 5.** A Level 1–3 employee, however
    otherwise eligible, cannot be assigned the lead-server position; capability level is a
    hard gate for this specific role, not just a fairness tie-break.
- Events whose title contains "Workshop" (e.g. the Ugly Stick Workshop) are a class, not a
  dinner-theatre show, and receive no staffing at all — the engine skips them before Stage 1
  demand is even calculated.

### Stage 3: Scarce Skill Protection
Reserves cross-trained staff (e.g., Bartender-capable staff, Yana/Kate 50/50 rotation) before ordinary Server positions are filled, ensuring scarce bar and 50/50 coverage is never compromised.

### Stage 4 allocation: global scarcity-ordered, deficit-driven assignment
Selection is **not** per-show greedy. Walking shows in date order and taking the
best-ranked candidate for each slot has two failure modes seen in production: it spends
flexible, always-available staff on early shows and starves later ones, and it lets
high-availability employees accumulate hours far past target while restricted-availability
staff sit near zero — because historical fairness metrics barely move within a single run.

The allocator (`scheduling/services/allocator.py`) plans the whole period at once:

1. **Build the full eligibility graph** for every (employee, slot) pair in the run before
   committing anything, so every option is known up front.
2. **Settle the most-constrained slot first** — fewest eligible candidates wins
   (minimum-remaining-values). This stops a slot's only possible candidate from being
   spent on a slot that had alternatives.
3. **Select by live deficit**, recomputed after every assignment:
   `0.50 x hours-deficit + 0.20 x opportunity-scarcity + 0.15 x shift-count-deficit
   + 0.12 x carry-in-history + 0.10 x Spirit-only + 0.05 x weekend-balance`, less a
   consecutive-night penalty and a strong over-target penalty. On-call is scored against
   its own separate budget.

The **carry-in-history** term is what stops each run starting everyone from zero. Within-run
deficit alone would give someone who just worked a heavy stretch the same standing as
someone who barely worked, because the run's own tallies begin empty. It scores
`Employee.opening_recent_hours` inverse-normalised across the pool, so whoever worked least
last period ranks highest. It is deliberately a tilt rather than a veto — a heavy carry-in
lowers priority but never makes anyone ineligible, so it cannot override availability,
qualification, or scarce-skill protection. The term is inert while carry-in hours are all
zero, and becomes active once actual Square timecard history is loaded.

Two consequences worth stating explicitly:

- **The Spirit-only boost (Olena, Jackie) is capped and self-cancelling.** It scales with
  their remaining deficit, so it decays to zero the moment they reach target — it improves
  their opportunity without letting them monopolise. Spirit-only staff are also eligible
  for on-call, scored against the same separate on-call budget as everyone else.
- **The Yana/Kate 50/50 rotation is now a tie-break, not the driver.** A blind alternation
  hands a slot to whoever is "next in sequence" even when the other candidate can work only
  that one date; deficit and scarcity lead, and rotation order settles genuine ties. This is
  the "resume the fairest rotation rather than restarting blindly" behaviour the spec calls
  for.

Every candidate considered for every slot still gets a `SchedulingFairnessSnapshot` with the
full component breakdown, so any decision remains auditable.

### Shift Timing
Each role's shift window is anchored to that specific show's own doors-open and wrap-up
times (read from the live show-calendar/show-detail page — doors, Act I, dinner, Act II),
not a fixed clock time shared across every show. Setup/wind-down buffers around doors-open
and wrap-up: Bartender 60 min before doors, Lead Server 45 min before, Busser and other
confirmed Server/50/50 roles 30 min before (matching most staff's actual confirmed
availability blocks); all confirmed roles run 15 min past wrap (Busser 30 min past, to
cover dinner-service wind-down). On-call roles (on-call-server, on-call-bartender) use
no buffer at all — exactly doors to wrap — since on-call staff are on standby for the
show itself, not arriving early for setup. The 50/50 role (Yana/Kate: Server + Office
Support) is the one exception to per-show anchoring — it stays a fixed nightly
6:00–9:30pm dinner-service window regardless of each show's own doors/wrap time, since
it's a supplementary office-hybrid slot, not full-shift floor coverage. Paid/on-call
hours are computed from this actual window, not a static per-template value.

### Target Hour Bands
Per pay-period confirmed-hours targets, applied through the existing
`EmployeeSchedulingPreference.target_hours` mechanism (Section on Target Hours Adjustment)
so the same capped, non-overriding adjustment used for Olena/Jackie now applies to everyone:
- **Server:** 40–50 hrs (target 45)
- **Busser:** 20–24 hrs (target 22)
- **Bartender** (incl. cross-trained Bartender+Server): 40–60 hrs (target 50)

The adjustment nudges ranking toward the target; it never overrides availability,
qualification, role coverage, or scarce-skill protection.

### Stage 4: Fairness Ranking & Selection
Computes explainable Fair Opportunity Scores for eligible candidates using configurable rolling history windows.

---

## Confirmed-Shift Fairness Score Formula

$$\text{FairScore} = 0.30 \times \text{OppFair} + 0.25 \times \text{HoursFair} + 0.10 \times \text{RoleOppFair} + 0.10 \times \text{ShiftFair} + 0.08 \times \text{WkndFair} + 0.05 \times \text{RestFair} + 0.05 \times \text{Reliability} + 0.04 \times \text{Performance} + 0.03 \times \text{RoleFit} + \text{TargetHoursAdjustment}$$

### Score Components & Weights:
1. **Eligible Opportunity Fairness (30%)**: Inverse normalization of $\text{OpportunityRate} = \frac{\text{Confirmed Opportunities}}{\text{Eligible Opportunities}}$.
2. **Recent Actual Paid-Hours Fairness (25%)**: Inverse normalization of worked hours across the rolling 28-day window.
3. **Role-Specific Opportunity Fairness (10%)**: Opportunity balance within the particular role.
4. **Recent Confirmed-Shift Fairness (10%)**: Inverse normalization of total shift count in rolling 28-day window.
5. **Weekend Fairness (8%)**: Inverse normalization of Friday/Saturday/Sunday shifts in rolling 56-day (8-week) window.
6. **Rest / Consecutive-Shift Fairness (5%)**: Soft rest penalty for consecutive nights (0 nights: 1.0, 1 night: 0.8, 2 nights: 0.5, 3+ nights: 0.2).
7. **Reliability (5%)**: Evidence-based attendance score (default 0.50 neutral when no data).
8. **Execution Performance (4%)**: Service execution quality (default 0.50 neutral when no data).
9. **Capability / Role-Fit (3%)**: Small tie-breaker for capability level.
10. **Target Hours Adjustment**: Additive priority bonus for Spirit-only priority staff (Olena & Jackie Pynn) when below target hours.

---

## Separate On-Call Ranking Score Formula

On-call is a separate evening burden and uses a dedicated ranking score:

$$\text{OnCallFairScore} = 0.40 \times \text{OnCallCountFair} + 0.20 \times \text{OnCallHoursFair} + 0.15 \times \text{OppFair} + 0.10 \times \text{HoursFair} + 0.05 \times \text{WkndFair} + 0.05 \times \text{Reliability} + 0.05 \times \text{RoleFit}$$

---

## Deterministic Lexicographic Tie-Break

When candidates are mathematically equal, tie-breaking order is completely deterministic:
1. Lower recent opportunity rate
2. Lower recent actual hours
3. Lower on-call burden
4. Lower employee display name (case-insensitive string sort)

*No random numbers are used.*

---

## Reproducibility & Audit Snapshots

For every schedule generation run:
- A `SchedulingFairnessSnapshot` record is created for every evaluated candidate, storing exact score components, breakdown JSON, and selection reason.
- Management can inspect candidate ranking and exclusion reasons directly in the UI.
