# Spirit Scheduling Engine

An internal Django application for Spirit of Newfoundland Productions. Phase 1 establishes the local staff database, protected management UI, Git workflow, and a sandbox-only Square integration. It deliberately does not implement the scheduling optimizer or publish schedules.

## Project purpose

The eventual application will combine Spirit show dates, expected guest counts, employee availability, role capability, scarce-skill protection, and fairness history to recommend confirmed and on-call rosters. Square remains the operational source for team records, scheduled shifts, and timecards.

Phase 1 provides:

- protected Django management pages;
- employee, role, capability, and Square-location models;
- an idempotent starter-roster seed;
- reusable Square configuration and API client modules;
- read-only Sandbox location, team-member, job, and scheduled-shift operations;
- a controlled draft-shift command that defaults to dry run;
- unconditional blocking of Square production writes;
- automated tests, linting, and a tracked-secret check.

## Technology

- Python 3.12+
- Django 5.2-6.0 compatible
- SQLite for local development
- PostgreSQL-ready `DATABASE_URL` configuration
- `requests` and `python-dotenv`
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

Phase 1 refuses all Square production writes even when a production token is configured.

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

The command is safe to rerun. It creates 17 approved employees, 4 roles, and 21 role assignments. It does not create Square IDs. Deborah Sweetapple and John Harris are intentionally excluded from automatic staffing.

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
- `/admin/` - Django administration

No public signup route exists.

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

Phase 2, only after management approval, can add show dates, guest counts, availability ingestion, configurable staffing thresholds, scarce-skill protection, shortage detection, and explainable fairness ranking. Later phases can add manager review and overrides, controlled draft synchronization, timecard feedback, and parallel production validation.

The detailed future rules are recorded in [`docs/scheduling_rules.md`](docs/scheduling_rules.md). General Square availability is not assumed to be API-accessible; a later phase must prove that capability or add a local availability entry/import workflow.
