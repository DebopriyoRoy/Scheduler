#!/bin/bash
#
# Spirit Scheduling Engine - one-time installer for a Mac.
#
# Double-click this file. It installs the application into your home folder and
# leaves the USB drive free to be unplugged afterwards.
#
# The app is deliberately NOT run from the USB drive: SQLite corrupts if the drive
# is pulled mid-write, and the Python environment has to be built on the machine
# that runs it (virtual environments hardcode paths and compiled packages are tied
# to the Mac's processor type, so a copied environment does not work).

set -euo pipefail

STICK="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TARGET="$HOME/SpiritScheduler"
PYTHON_MIN="3.12"

bold() { printf "\033[1m%s\033[0m\n" "$1"; }
ok()   { printf "  \033[32m✓\033[0m %s\n" "$1"; }
warn() { printf "  \033[33m!\033[0m %s\n" "$1"; }
die()  { printf "\n  \033[31m✗ %s\033[0m\n\n" "$1"; echo "Press any key to close."; if [ -t 0 ]; then read -r -n 1; fi; exit 1; }

clear 2>/dev/null || true
bold "Spirit Scheduling Engine — installer"
echo "Installing to: $TARGET"
echo

# ---------------------------------------------------------------- 1. Python
bold "1. Checking Python"
PY=""
for candidate in python3.13 python3.12 python3; do
  if command -v "$candidate" >/dev/null 2>&1; then
    version="$("$candidate" -c 'import sys; print("%d.%d" % sys.version_info[:2])' 2>/dev/null || echo 0)"
    if [ "$(printf '%s\n%s\n' "$PYTHON_MIN" "$version" | sort -V | head -1)" = "$PYTHON_MIN" ]; then
      PY="$candidate"; break
    fi
  fi
done

if [ -z "$PY" ]; then
  warn "Python $PYTHON_MIN or newer was not found."
  echo
  echo "  Install it with either of these, then run this installer again:"
  echo "    • Homebrew:  brew install python@3.12"
  echo "    • Download:  https://www.python.org/downloads/macos/"
  die "Python $PYTHON_MIN+ is required."
fi
ok "Using $($PY -V) at $(command -v "$PY")"

# ---------------------------------------------------------------- 2. Files
bold "2. Copying application files"
mkdir -p "$TARGET"
rsync -a --delete \
  --exclude '.venv' --exclude '__pycache__' --exclude '*.pyc' \
  --exclude 'db.sqlite3' --exclude '.env' --exclude 'staticfiles' \
  "$STICK/source/" "$TARGET/"
ok "Application code copied"

# The database is never overwritten. A reinstall must not destroy live rosters.
if [ -f "$TARGET/db.sqlite3" ]; then
  ok "Existing database kept (not overwritten)"
elif [ -f "$STICK/db.sqlite3" ]; then
  cp "$STICK/db.sqlite3" "$TARGET/db.sqlite3"
  ok "Database copied from the USB drive"
else
  warn "No database found — a new empty one will be created"
fi

# ---------------------------------------------------------------- 3. Environment
bold "3. Building the Python environment (a few minutes)"
"$PY" -m venv "$TARGET/.venv"
"$TARGET/.venv/bin/python" -m pip install --quiet --upgrade pip
"$TARGET/.venv/bin/python" -m pip install --quiet -r "$TARGET/requirements.txt"
ok "Packages installed"

bold "4. Downloading the browser engine for calendar sync"
"$TARGET/.venv/bin/python" -m playwright install chromium >/dev/null 2>&1 \
  && ok "Chromium ready" \
  || warn "Chromium download failed — the live calendar sync will not work until you run: $TARGET/.venv/bin/python -m playwright install chromium"

# ---------------------------------------------------------------- 5. Settings
bold "5. Configuration"
if [ -f "$TARGET/.env" ]; then
  ok "Existing settings kept"
else
  SECRET="$("$TARGET/.venv/bin/python" -c 'import secrets; print(secrets.token_urlsafe(50))')"
  echo
  echo "  Your Square access token is needed to sync schedules."
  echo "  It is stored only on this Mac, never on the USB drive."
  echo "  Leave it blank to skip — the app still runs, Square sync stays disabled."
  echo
  printf "  Square production access token: "
  # Tolerate no terminal (piped/automated run): an empty token is a valid choice.
  read -r SQUARE_TOKEN || SQUARE_TOKEN=""
  SQUARE_TOKEN="${SQUARE_TOKEN:-}"
  echo

  cat > "$TARGET/.env" <<ENVEOF
DJANGO_SECRET_KEY=$SECRET
DJANGO_DEBUG=false
DJANGO_HTTPS=false
DJANGO_ALLOWED_HOSTS=127.0.0.1,localhost
DATABASE_URL=sqlite:///db.sqlite3

SQUARE_ENVIRONMENT=production
SQUARE_SANDBOX_ACCESS_TOKEN=
SQUARE_PRODUCTION_ACCESS_TOKEN=$SQUARE_TOKEN
SQUARE_LOCATION_ID=LR73BX986ZKYD
SQUARE_API_VERSION=2025-06-18
SQUARE_REQUEST_TIMEOUT_SECONDS=30
SQUARE_PRODUCTION_WRITES_ENABLED=false
SQUARE_PRODUCTION_PILOT_VERIFIED=true
SQUARE_PUBLISHING_ENABLED=false
ENVEOF
  chmod 600 "$TARGET/.env"
  ok "Settings written (readable only by you)"
fi

# ---------------------------------------------------------------- 6. Database
bold "6. Preparing the database"
cd "$TARGET"
"$TARGET/.venv/bin/python" manage.py migrate --noinput >/dev/null
ok "Database up to date"
"$TARGET/.venv/bin/python" manage.py collectstatic --noinput >/dev/null
ok "Interface files ready"

# ---------------------------------------------------------------- 7. Login
bold "7. Management login"
HAS_USER="$("$TARGET/.venv/bin/python" -c "
import django, os
os.environ.setdefault('DJANGO_SETTINGS_MODULE','spirit_scheduler.settings')
django.setup()
from django.contrib.auth import get_user_model
print('yes' if get_user_model().objects.exists() else 'no')
" 2>/dev/null || echo no)"

if [ "$HAS_USER" = "yes" ]; then
  ok "Existing login kept"
else
  if [ -t 0 ]; then
    echo "  Create the account you will sign in with."
    "$TARGET/.venv/bin/python" manage.py createsuperuser || warn "Skipped — create one later with: manage.py createsuperuser"
  else
    warn "No terminal for the login prompt — create one with: manage.py createsuperuser"
  fi
fi

cp "$STICK/START.command" "$TARGET/START.command" 2>/dev/null || true
cp "$STICK/BACKUP.command" "$TARGET/BACKUP.command" 2>/dev/null || true
chmod +x "$TARGET/START.command" "$TARGET/BACKUP.command" 2>/dev/null || true

echo
bold "Done."
echo
echo "  To start the application:  open $TARGET and double-click START.command"
echo "  A shortcut has also been placed in that folder for you."
echo
echo "  You can now safely eject the USB drive."
echo
if [ -t 0 ]; then echo "Press any key to close."; read -r -n 1; fi
