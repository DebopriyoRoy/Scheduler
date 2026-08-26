#!/bin/bash
#
# Copy the live database onto the USB drive.
#
# That single file holds every show, roster, availability record and audit note.
# Nothing else in the installation is irreplaceable; this is.

set -euo pipefail

TARGET="$HOME/SpiritScheduler"
DB="$TARGET/db.sqlite3"

bold() { printf "\033[1m%s\033[0m\n" "$1"; }

clear 2>/dev/null || true
bold "Spirit Scheduling Engine — backup"
echo

[ -f "$DB" ] || { printf "\033[31mNo database found at %s\033[0m\n\n" "$DB"; if [ -t 0 ]; then read -r -n 1; fi; exit 1; }

# Prefer the USB drive this was launched from; fall back to the Desktop.
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
case "$HERE" in
  /Volumes/*) DEST="$HERE/backups" ;;
  *)          DEST="$HOME/Desktop/SpiritScheduler-backups" ;;
esac
mkdir -p "$DEST"

STAMP="$(date +%Y-%m-%d_%H%M)"
OUT="$DEST/db-$STAMP.sqlite3"

# .backup produces a consistent copy even while the app is running; a plain cp of a
# live SQLite file can capture a half-written transaction.
if command -v sqlite3 >/dev/null 2>&1; then
  sqlite3 "$DB" ".backup '$OUT'"
else
  cp "$DB" "$OUT"
fi

SIZE="$(du -h "$OUT" | cut -f1)"
printf "  \033[32m✓\033[0m Saved %s (%s)\n\n" "$OUT" "$SIZE"

COUNT="$(ls -1 "$DEST"/db-*.sqlite3 2>/dev/null | wc -l | tr -d ' ')"
echo "  $COUNT backup(s) in $DEST"
echo
if [ -t 0 ]; then echo "Press any key to close."; read -r -n 1; fi
