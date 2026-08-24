# Spirit Scheduling Engine

An internal Django application for Spirit of Newfoundland Productions. Phase 2 provides a protected, browser-based workflow for importing shows, recording availability, generating deterministic staffing recommendations, reviewing warnings and workload, making audited overrides, approving locally, and exporting management reports.

## Project purpose

The application combines Spirit show dates, expected guest counts, employee availability, role capability, scarce-skill protection, rotation rules, and fairness history to recommend confirmed and on-call rosters. Square remains the operational source for team records, scheduled shifts, and timecards.

Phase 2 provides:

- protected Django management pages;
- employee, role, capability, and Square-location models;
- an idempotent starter-roster seed;
- reusable Square configuration and API client modules;
- read-only Sandbox location, team-member, job, and scheduled-shift operations;
- a controlled draft-shift command that defaults to dry run;
- unconditional blocking of Square production writes;
- show calendar import with manual-entry fallback;
- local availability entry and validated CSV preview/import;
- configurable office and 50/50 rotation seeds;
- a deterministic, explainable constraint-based scheduling engine;
- explicit shortage and data-quality warnings;
- audited valid-only management overrides and local approval;
- Excel, CSV, and PDF exports;
- automated tests, linting, and a tracked-secret check.

## Technology

- Python 3.12+
- Django 5.2-6.0 compatible
- SQLite for local development
- PostgreSQL-ready `DATABASE_URL` configuration
- `requests`, `python-dotenv`, and Beautiful Soup
- openpyxl Excel and ReportLab PDF exports
- pytest, pytest-django, and Ruff
- Bootstrap 5 management UI

## Local setup

```bash
git checkout spirit-scheduling-development
python3.12 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
cp .env.example .env
```

Keep `.env` local. It is intentionally ignored by Git.

### Environment variables

```dotenv
DJANGO_SECRET_KEY=replace-with-a-long-local-secret
DJANGO_DEBUG=true
DJANGO_ALLOWED_HOSTS=127.0.0.1,localhost
DATABASE_URL=sqlite:///db.sqlite3

SQUARE_ENVIRONMENT=sandbox
SQUARE_SANDBOX_ACCESS_TOKEN=
SQUARE_PRODUCTION_ACCESS_TOKEN=
SQUARE_LOCATION_ID=
SQUARE_API_VERSION=2026-08-19
SQUARE_REQUEST_TIMEOUT_SECONDS=15
```

Phase 2 continues to refuse all Square production writes even when a production token is configured.

## Database migrations

```bash
python manage.py migrate
```

For PostgreSQL, install or start PostgreSQL and set a URL such as:

```dotenv
DATABASE_URL=postgresql://spirit_scheduler:password@localhost:5432/spirit_scheduler
```

The included `psycopg` dependency allows Django to use that database without changing application code.

## Seed the starter roster

```bash
python manage.py seed_spirit_staff
```

The command is safe to rerun. It creates 17 approved employees, 4 roles, and 22 active role assignments. It does not create Square IDs. Debroah Sweetapple and John Haris (including corrected spelling variants) are explicitly blocked from automatic staffing.

Seed the scheduling templates and rules:

```bash
python manage.py seed_scheduling_config
```

For isolated local development only, `python manage.py seed_schedule_demo` creates clearly marked demo shows, all-day demo availability, and both rotation seeds. It runs only with `DEBUG=True` and refuses to overwrite non-demo records on its target dates.

## Create an admin user

```bash
python manage.py createsuperuser
```

## Run the application

```bash
python manage.py runserver
```

Open `http://127.0.0.1:8000/`, sign in with the Django management account, and use:

- `/` - management dashboard
- `/employees/` - local staff and capability mappings
- `/roles/` - local roles and Square job mapping status
- `/integrations/square/` - Sandbox connection and locations
- `/shows/` - import, add, edit, and deactivate shows
- `/availability/` - local availability entry and CSV import
- `/configuration/rotations/` - office and 50/50 rotation seeds
- `/schedules/` - generate, review, approve, and export schedule versions
- `/admin/` - Django administration

No public signup route exists.

## Phase 2 features

### Show management and guest counts

Use **Shows** to import public Spirit calendar pages for any date range or add a show manually when the public site is unavailable. Imports update only their own external IDs and never delete manual records. Expected guests are editable per show. A missing value is clearly treated as the 100-person planning default. Counts above a configured staffing range create `HIGH_GUEST_COUNT_REVIEW`; theatre capacity is 175 unless an administrator records an override reason.

For command-line import during setup or diagnostics:

```bash
python manage.py import_show_calendar --start 2026-09-07 --end 2026-10-03
```

### Availability

Use **Availability** to select a date and record each active employee as available all day, available for a time window, unavailable, or unknown. A date's entries can be copied to additional comma-separated dates. Unknown availability is always ineligible; generation never silently treats it as available.

### Availability CSV import

Download the template for a schedule period, fill the columns `employee,date,available,start_time,end_time,notes`, then upload it. The complete file is validated and shown as a preview before confirmation. Unknown employees, duplicate employee/date rows, malformed dates/times, and invalid windows reject the whole file; no partial import occurs.

The equivalent setup command is:

```bash
python manage.py import_availability path/to/availability.csv
```

### Office rotation

Set a Saturday seed and choose Yana or Khrystyna. The selected person works the seed Saturday, the other works Sunday, and the next weekend swaps. Office work blocks a show assignment only when the time windows overlap.

### 50/50 rotation

Choose Yana or Kate as the seed. When both are eligible, they alternate 50/50 across shows; when only one is eligible, that person is used without advancing the two-person alternation. A 50/50 employee cannot also hold another role in the same show.

### Generating a schedule

Open **Schedules → Generate Schedule**. The initial dates are September 7 through October 3, 2026, but any range is editable. Before generation, the page shows show count, guest-count coverage, defaulted guest counts, availability completion, employees available, and missing entries. Incomplete availability requires the explicit **Generate with shortages** choice.

The deterministic constraint engine fills exactly these standard positions for a 1–100 guest show:

- Lead Server, Server 2, Server 3, and On-Call Server;
- Bartender and On-Call Bartender;
- Busser;
- 50/50 when the show requires it.

Hard constraints cover availability, role qualification, manager exclusion, overlap, one role per person/show, office overlap, and bartender protection. Soft ordering uses separately visible confirmed hours, confirmed shifts, on-call burden, weekend burden, recent consecutive nights, and Spirit-only opportunity priority for Olena and Jackie. Every assignment records a plain-language selection reason.

### Understanding warnings

The review page displays shortages, unknown availability, default guest counts, high guest-count review, office overlap, incomplete fairness history, and configuration errors. The engine never hides a shortage by assigning an ineligible person. Hard errors must be corrected or resolved with a management note before approval.

### Manual overrides

Use **Replace** on an assignment. A reason of at least five characters is mandatory. The replacement must still pass qualification, availability, overlap, office, and one-role-per-show checks. Phase 2 has no forced-invalid override.

### Approving a schedule

**Approve Schedule** records the approving user and timestamp only when no unresolved hard errors remain. An approved version cannot be regenerated or silently modified. Use **Create New Draft** to produce another version.

### Exporting Excel, CSV, and PDF

The review page exports:

- an openpyxl workbook with `Schedule`, `Detailed Assignments`, `Employee Totals`, `Warnings`, and `Assumptions` sheets;
- a detailed UTF-8 CSV;
- a management-friendly ReportLab PDF with schedule, warnings, workload totals, and assumptions.

All exported times are rendered in `America/St_Johns` local time. Exports contain no tokens or environment secrets.

### Current limitations

- Public calendar pages can change structure or block automated requests; manual show entry remains the supported fallback.
- Guest counts and employee availability must be supplied by management; Phase 2 does not invent either beyond the explicit 100-person default.
- Prior timecard history is not yet imported from Square. Management can enter opening recent hours and shift counts; incomplete history is warned.
- Staffing above 100 guests uses the highest approved rule and requires management review. A final high-capacity/Christmas matrix still requires approval.
- No employee notification, payroll, timecard replacement, or mobile application is included.

### Square Production status

**Square Production synchronization is NOT enabled in Phase 2.** Schedule generation, approval, and export are local. The application does not publish schedules or create Production shifts. The Phase 1 controlled Sandbox draft command remains isolated and is not called by the management workflow.

## Square Sandbox setup

1. Create or select a Square Sandbox application in the Square Developer Console.
2. Obtain the Sandbox access token. Never paste it into source code, Git, logs, screenshots, or chat.
3. Put the token in the ignored local `.env` as `SQUARE_SANDBOX_ACCESS_TOKEN`.
4. Keep `SQUARE_ENVIRONMENT=sandbox`.
5. Grant the application permissions required by the chosen operations. Location reads require `MERCHANT_PROFILE_READ`; team members and jobs require `EMPLOYEES_READ`; scheduled-shift reads and writes use the Labor API permissions, including `TIMECARDS_READ` or `TIMECARDS_WRITE` as applicable.

Square Sandbox calls use `https://connect.squareupsandbox.com`. Production reads would use `https://connect.squareup.com`, but Phase 1 commands intentionally require Sandbox.

## Test the Square connection and list locations

```bash
python manage.py square_test_connection
```

A successful response prints `Square connection: SUCCESS`, followed by each location name, ID, and status. Tokens and authorization headers are never printed.

## List active team members

```bash
python manage.py square_list_team
```

The command prints given name, family name, Square team-member ID, and status. It does not automatically map production staff IDs.

## List jobs

```bash
python manage.py square_list_jobs
```

The command prints job title and ID and reports expected Spirit jobs that are absent. It never creates missing Square jobs.

## Search scheduled shifts in application code

`SquareClient.search_scheduled_shifts(query)` exposes the Labor API search operation through the centralized integration layer. Raw Square HTTP calls should not be added to Django views or business models.

## Run the optional read-only Sandbox integration checks

```bash
python manage.py square_sandbox_integration_test
```

This command reads locations, active team members, and jobs. It performs zero writes.

## Create one Sandbox draft shift

First run the command without `--confirm`:

```bash
python manage.py square_create_test_draft_shift \
  --team-member-id TEAM_MEMBER_ID \
  --job-id JOB_ID \
  --location-id LOCATION_ID \
  --start "2026-09-10T17:00:00-02:30" \
  --end "2026-09-10T23:00:00-02:30"
```

The result is a dry run and makes no Square changes. After checking every ID and time, append `--confirm` to create exactly one Sandbox draft:

```bash
python manage.py square_create_test_draft_shift \
  --team-member-id TEAM_MEMBER_ID \
  --job-id JOB_ID \
  --location-id LOCATION_ID \
  --start "2026-09-10T17:00:00-02:30" \
  --end "2026-09-10T23:00:00-02:30" \
  --confirm
```

The command uses a deterministic idempotency key derived from the IDs and timestamps. Repeating the exact request does not intentionally create a duplicate. It calls only Square's draft-shift creation endpoint; no publish endpoint exists in this application.

## Tests and quality checks

```bash
pytest
ruff check .
python manage.py check
python manage.py makemigrations --check --dry-run
python scripts/check_secrets.py
```

All automated tests mock Square requests. They never create real scheduled shifts.

## Security rules

- Load all secrets from environment variables.
- Never commit `.env`, tokens, passwords, cookies, keys, browser profiles, or session data.
- Never log an access token or authorization header.
- Keep production Square writes blocked throughout Phase 1.
- Do not add schedule-publishing endpoints or commands.
- Run `python scripts/check_secrets.py` before each push.
- Review `git status` and `git diff --cached` before committing.

## Git workflow

Development occurs on `spirit-scheduling-development`:

```bash
git checkout spirit-scheduling-development
git pull origin spirit-scheduling-development
git add .
git commit -m "describe the focused change"
git push origin spirit-scheduling-development
```

Use small commits. Never force-push this branch or rewrite shared history.

## Home development workflow

For a first checkout:

```bash
git clone <repository-url>
cd <repository-directory>
git checkout spirit-scheduling-development
```

For an existing checkout:

```bash
git checkout spirit-scheduling-development
git pull origin spirit-scheduling-development
```

After local work:

```bash
pytest
ruff check .
python scripts/check_secrets.py
git add .
git commit -m "..."
git push origin spirit-scheduling-development
```

## macOS, Docker, and Mac mini direction

The application is currently runnable on macOS with Python 3.12 and SQLite. Environment-based settings, a WSGI/ASGI entry point, static-file collection, and PostgreSQL URL support allow a later container or Mac mini deployment without changing application logic. A production deployment still needs an approved web server, TLS termination, `DJANGO_DEBUG=false`, a strong secret key, approved hosts, backups, and a deployment runbook.

## Future development phases

Phase 3 should add an explicit, separately approved path that maps an **approved local schedule version** to Square Sandbox draft shifts, previews every payload and conflict, uses idempotency keys, and preserves the current unconditional Production-write block. Only after Sandbox validation and a separate management decision should parallel Production read validation be considered.

The detailed rules are recorded in [`docs/scheduling_rules.md`](docs/scheduling_rules.md). General Square availability is not assumed to be API-accessible; the Phase 2 provider abstraction allows a proven Square adapter to supplement or replace local availability later without coupling the scheduling engine to the database model.
