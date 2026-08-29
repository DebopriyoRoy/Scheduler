# Why a person with availability still does not appear on a schedule

Having hours in Square is necessary, not sufficient. A person must clear **every** gate
below for a given position; failing one is enough to leave that position empty. The
gates are in the order the engine applies them, in `scheduling/services/eligibility.py`
and `scheduling/services/availability.py`.

The single most common cause of a schedule that looks completely broken — every server
column empty, dozens of shortages — is **number 7**, and it is not a fault.

---

### 1. The person is inactive

`Employee.active` is false. They stay on the roster for history and never get work.

### 2. They are excluded from automatic scheduling

`excluded_from_automatic_scheduling`. Set for managers and the owner when Square's team
is imported, because their Square job is Manager or Owner. They can still be added to a
shift by hand.

### 3. They do not hold the role

A Busser is not eligible for a Server position. Roles come from each person's **Square
job assignments** and nowhere else — `import_square_team` maps Square's `Service`,
`Bartender`, `Busser` and `50/50` jobs onto the four roles here. Someone whose Square
job is Kitchen, Chef, Cleaner, Tech or the generic Team Member holds **no role**, so
they appear on the roster and can never be rostered. That is deliberate.

### 4. A busser is barred from alcohol-adjacent roles

Bussers are the under-19 position and cannot hold Server, Bartender or 50/50, even if a
data-entry error grants them one.

There is deliberately **no seniority gate on server positions**. Square carries a single
`Service` job with no lead grade, and the published rosters show Level 3 staff working
the earliest and longest floor shifts. The positions differ in when they start, not in
rank.

### 5. Availability does not cover the shift

Three distinct states, and the difference matters:

| State | Meaning | Schedulable |
|---|---|---|
| `AVAILABLE_ALL_DAY` | Square says "All day" | Yes |
| `AVAILABLE_WINDOW` | e.g. `18:00–23:00` | Only if a window **fully covers** the shift |
| `UNAVAILABLE` | explicitly marked unavailable | No |
| `UNKNOWN` | **Square holds nothing for that day** | No |

`UNKNOWN` is not the same as unavailable. Both end unschedulable, but only one is worth
asking a person about — an empty day usually means nobody has filled their availability
in, not that they refused it.

Coverage is **full coverage**, not overlap. A 17:30–21:30 window does not cover an
18:00–22:30 shift; it ends an hour early.

A person may hold **several windows on one day** — Square shows each as its own row, and
the check passes if *any* one of them covers the shift. A window whose end is at or
before its start (`14:30–00:00`) is a window running to or past midnight, not a broken
record.

### 6. Approved time off

An approved time-off request covering the date, all day or overlapping the shift hours.

### 7. They are already working — the big one

Two separate checks:

- **An existing schedule assignment overlaps this shift.** Someone rostered on another
  run that is `APPROVED` or `SYNCED_TO_SQUARE` cannot be booked again at the same time.
  Nobody can work two shifts at once.
- **Already assigned another role for this show.** One person, one job per show.

A run that is `DRAFT`, `NEEDS_REVIEW` or `SUPERSEDED_SOURCE_DATA` books nobody, so it
never blocks.

**This is what makes an overlapping run look broken.** Generate a schedule across dates
an approved or synced roster already covers and almost every position will be a
shortage — correctly, because those people are already working. The engine now says so
in a banner at the top of the schedule naming the run responsible. To re-plan those
dates, edit the live run or supersede it first; generating a second one over the top
cannot work.

### 8. An office assignment overlaps

The office rotation puts someone in the office during the shift.

---

## Reading it off a real schedule

Every empty position carries a **Why?** link listing the exact reasons for the four
nearest candidates. When a whole column is empty, check the banner at the top first —
one overlap explains the entire page far faster than opening forty-six identical
warnings.

To get the full picture across a run, in a shell:

```python
from scheduling.models import ScheduleRun, SchedulingWarning, ShiftTemplate, Employee
from scheduling.services.engine import SchedulingEngine

run = ScheduleRun.objects.get(pk=33)
engine = SchedulingEngine()
templates = {t.name: t for t in ShiftTemplate.objects.filter(active=True)}

for w in SchedulingWarning.objects.filter(schedule_run=run, warning_type__endswith="SHORTAGE"):
    name = w.message.split("No eligible employee for ", 1)[-1].split(".")[0].strip()
    template = templates.get(name)
    if not template:
        continue
    start, end = engine._datetimes(w.show, template)
    for person in Employee.objects.filter(
        active=True, employee_roles__role=template.role, employee_roles__active=True
    ):
        result = engine.eligibility.evaluate(
            person, template.role, w.show, template, run, start, end
        )
        if not result.eligible:
            print(w.show.date, name, person.display_name, result.reasons)
```

## What to do about the common ones

| Symptom | Fix |
|---|---|
| Whole run is shortages | Check the overlap banner. Edit or supersede the live run instead |
| One role is always short | Square holds no availability for the people who hold it — ask them to set it, then Sync |
| Someone is never picked | Open Why?; usually a window that ends before the shift does |
| A new hire never appears | Their Square job maps to no role here. Check `import_square_team` output |
