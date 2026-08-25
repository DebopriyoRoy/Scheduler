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
Calculated independently from employee ranking based on show guest count (e.g. approved 100-guest baseline: 3 Confirmed Servers, 1 On-Call Server, 1 Confirmed Bartender, 1 On-Call Bartender, 1 Confirmed Busser, 1 50/50 employee when applicable).

### Stage 2: Hard Eligibility Filter
Evaluates mandatory operational constraints before any candidate receives a fairness score:
- Active roster employee (managers Deborah Sweetapple & John Harris are excluded)
- Authoritative role qualification
- Continuous full-shift availability (UNKNOWN availability is ineligible)
- No office time overlap
- No existing Square shift or theatre shift conflict
- Legal and role restrictions

### Stage 3: Scarce Skill Protection
Reserves cross-trained staff (e.g., Bartender-capable staff, Yana/Kate 50/50 rotation) before ordinary Server positions are filled, ensuring scarce bar and 50/50 coverage is never compromised.

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
