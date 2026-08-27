# Putting Spirit Scheduler on another Mac

Two different jobs, and they need different things. Doing only the first gives you a
working app that cannot be changed; doing only the second gives you a machine that can
build the app but has not installed it.

| You want to | Do this | Takes |
|---|---|---|
| **Use** the scheduler on that Mac | Install the `.dmg` | ~5 minutes |
| **Develop and test** on that Mac too | Clone the repository and run the setup script | ~15 minutes |

Both Macs are Apple Silicon (M4 Pro), so one build serves both. PyInstaller cannot
cross-compile: a build made on Apple Silicon will not launch on an Intel Mac, and
would have to be rebuilt there.

---

## 1. Install the application

Copy **`dist/Spirit Scheduler.dmg`** across — USB stick, AirDrop, anything — and open
it. Drag the app onto Applications, then read `READ ME FIRST.txt` inside the disk
image. The important step is the second one:

```bash
xattr -dr com.apple.quarantine "/Applications/Spirit Scheduler.app"
```

The app is signed ad-hoc rather than notarised by Apple, so macOS blocks it on first
launch — silently, with no error and no message. That one line clears it, once. It is
not a workaround for a broken app; notarisation needs a paid Apple Developer account.

**First run downloads a browser.** The show calendar is rendered by JavaScript, and
Square publishes no availability API, so both are read by driving a real Chromium.
It is about 150 MB, fetched once into `~/Library/Caches/ms-playwright`, and needs an
internet connection. Until it lands, syncing will not work.

### What the installed app keeps, and where

Everything lives in `~/Library/Application Support/Spirit Scheduler/`:

- `db.sqlite3` — shows, staff, availability, every schedule run
- `square-session/` — the signed-in Square dashboard session
- `app.log` — everything the app printed, which is where to look first when something
  misbehaves

A fresh Mac starts with an empty database. **Copy `db.sqlite3` across** if you want the
same shows and rosters; leave it out for a clean start. Copy it while the app is
closed on both machines, or SQLite may be mid-write.

**Do not copy `square-session/`.** It holds live session cookies. Press *Connect to
Square* on the new machine and sign in there — it takes thirty seconds and is the only
way the new machine gets a session it actually owns.

---

## 2. Set the Mac up for development

```bash
git clone https://github.com/DebopriyoRoy/Scheduler.git
cd Scheduler
git checkout spirit-scheduling-development
./scripts/setup-dev-mac.sh
```

The script needs Python 3.12 (`brew install python@3.12` if it is missing). It creates
the virtualenv, installs the pinned dependencies, fetches Chromium, migrates and seeds
a development database, and then **runs the whole test suite** — so the machine proves
itself before you trust it. If the tests fail, it stops and says so.

Three things are deliberately *not* in the repository and have to be set up by hand:

**The virtualenv.** `.venv/` bakes in absolute paths and does not survive being copied.
The script builds a fresh one.

**Your Square credentials.** Copy `.env.example` to `.env` and paste your own values in.
`.env` is git-ignored because it carries a live production access token — it must never
be committed, and a token that has been pasted into a chat, an email or a document
should be rotated in the Square dashboard before you use it.

**The Square dashboard session.** Run this once, sign in to Square in the window that
opens, and leave it alone until the dashboard appears:

```bash
.venv/bin/python manage.py square_connect
```

Your password goes to Square's own page. Nothing here ever sees or stores it. Square
expires the session every few hours, so expect to do this again — from inside the app
there is a **Connect to Square** button on the Staff & Availability page.

### Working on it day to day

```bash
.venv/bin/python manage.py runserver 8765     # the app at 127.0.0.1:8765
.venv/bin/python manage.py createsuperuser    # a login for yourself
.venv/bin/python -m pytest -q                 # the test suite
.venv/bin/python -m ruff check scheduling/    # lint
./desktop/build_app.sh                        # rebuild the .dmg on this machine
```

### Keeping both Macs in step

The repository is the shared copy; the databases are not. Commit and push on one
machine, pull on the other:

```bash
git push origin spirit-scheduling-development
```

Scheduling data does not travel through git — `db.sqlite3` is ignored on purpose, so a
half-finished roster on one Mac never overwrites a published one on the other. Move a
database only by copying the file deliberately, with both apps closed.

---

## Which database am I looking at?

This catches people out, because there are two and they are not the same file.

| Started with | Database |
|---|---|
| The installed `.app` | `~/Library/Application Support/Spirit Scheduler/db.sqlite3` |
| `manage.py runserver` | `db.sqlite3` inside the repository |

To develop against the real data, point the dev server at the app's own database:

```bash
DATABASE_URL="sqlite:////Users/$USER/Library/Application Support/Spirit Scheduler/db.sqlite3" \
  .venv/bin/python manage.py runserver 8765
```

Four slashes: three for the URL scheme, one for the absolute path. Close the app first
— two processes writing one SQLite file will eventually collide.
