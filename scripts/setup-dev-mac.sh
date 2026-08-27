#!/bin/bash
#
# Set this repository up for development on a Mac that has never run it.
#
#   ./scripts/setup-dev-mac.sh
#
# Creates the virtualenv, installs the pinned dependencies, fetches the Chromium
# build Playwright drives, prepares the database, and runs the test suite so the
# machine proves itself before anyone relies on it.
#
# Deliberately not copied between machines: the virtualenv (it bakes in absolute
# paths), the database (it is real scheduling data - see docs/second-mac.md), and the
# Square dashboard session (it holds live session cookies; sign in on the new machine
# instead).

set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO"

echo "Spirit Scheduler - development setup"
echo "  repository: $REPO"
echo

# ---------------------------------------------------------------- interpreter
# 3.12 is what the application is pinned and tested against. Django 6 needs 3.12+,
# and a mismatch here surfaces much later as an obscure import error.
PY_BIN=""
for candidate in python3.12 /opt/homebrew/bin/python3.12 python3; do
  if command -v "$candidate" >/dev/null 2>&1; then
    version="$("$candidate" -c 'import sys; print("%d.%d" % sys.version_info[:2])' 2>/dev/null || echo "")"
    case "$version" in
      3.12|3.13) PY_BIN="$candidate"; break ;;
    esac
  fi
done

if [ -z "$PY_BIN" ]; then
  echo "  Python 3.12 was not found." >&2
  echo "  Install it first:  brew install python@3.12" >&2
  echo "  (Homebrew itself: https://brew.sh)" >&2
  exit 1
fi
echo "1. Interpreter: $("$PY_BIN" --version) at $(command -v "$PY_BIN")"

# ------------------------------------------------------------------ virtualenv
if [ -d .venv ]; then
  echo "2. Virtualenv already present - reusing it"
else
  echo "2. Creating virtualenv"
  "$PY_BIN" -m venv .venv
fi
PY="$REPO/.venv/bin/python"

echo "3. Installing pinned dependencies (a few minutes on a first run)"
"$PY" -m pip install --quiet --upgrade pip
"$PY" -m pip install --quiet -r requirements.txt
echo "   done"

# --------------------------------------------------------------------- browser
# The show calendar is rendered by JavaScript and Square publishes no availability
# API, so both are read by driving a real browser. The binary lives outside the
# virtualenv, in ~/Library/Caches/ms-playwright, and is ~150 MB.
echo "4. Fetching the Chromium build Playwright drives"
if [ -n "$(find "$HOME/Library/Caches/ms-playwright" -maxdepth 1 -name 'chromium*' 2>/dev/null | head -1)" ]; then
  echo "   already installed"
else
  "$PY" -m playwright install chromium >/dev/null
  echo "   done"
fi

# -------------------------------------------------------------------- database
echo "5. Preparing the development database"
"$PY" manage.py migrate --noinput >/dev/null
"$PY" manage.py seed_spirit_staff >/dev/null
"$PY" manage.py seed_scheduling_config >/dev/null
echo "   migrated and seeded (db.sqlite3 in the repository - development only)"

# ------------------------------------------------------------------ self-check
echo "6. Running the test suite"
if "$PY" -m pytest -q 2>&1 | tail -3; then
  echo
else
  echo "   TESTS FAILED - stop here and read the output above." >&2
  exit 1
fi

cat <<'NEXT'
Ready.

  Start the development server
      .venv/bin/python manage.py runserver 8765

  Create a login for yourself (it will ask for a password; nothing stores it here)
      .venv/bin/python manage.py createsuperuser

  Connect to Square, once, so availability and the calendar can be read
      .venv/bin/python manage.py square_connect

  Build the installable app for this architecture
      ./desktop/build_app.sh

The development database is empty of real shows and rosters. To work against the
real data instead, see docs/second-mac.md.
NEXT
