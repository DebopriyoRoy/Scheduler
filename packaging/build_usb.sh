#!/bin/bash
#
# Assemble the USB drive folder from this working copy.
#
#   ./packaging/build_usb.sh /Volumes/YOUR_USB_NAME
#
# Omit the destination to build into ~/Desktop/SpiritScheduler-USB and copy it across
# by hand.

set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DEST="${1:-$HOME/Desktop/SpiritScheduler-USB}"

echo "Building USB package"
echo "  from : $REPO"
echo "  to   : $DEST"
echo

rm -rf "$DEST"
mkdir -p "$DEST/source"

rsync -a \
  --exclude '.venv' --exclude '.git' --exclude '__pycache__' --exclude '*.pyc' \
  --exclude '.env' --exclude 'staticfiles' --exclude 'db.sqlite3' \
  --exclude '.pytest_cache' --exclude '.ruff_cache' --exclude '*.pdf' \
  --exclude 'artifacts' --exclude 'packaging' \
  "$REPO/" "$DEST/source/"

cp "$REPO/packaging/INSTALL.command" "$DEST/"
cp "$REPO/packaging/START.command"   "$DEST/"
cp "$REPO/packaging/BACKUP.command"  "$DEST/"
cp "$REPO/packaging/READ-ME-FIRST.txt" "$DEST/"
chmod +x "$DEST"/*.command

# The database travels; the secrets file never does.
if [ -f "$REPO/db.sqlite3" ]; then
  if command -v sqlite3 >/dev/null 2>&1; then
    sqlite3 "$REPO/db.sqlite3" ".backup '$DEST/db.sqlite3'"
  else
    cp "$REPO/db.sqlite3" "$DEST/db.sqlite3"
  fi
  echo "  database included ($(du -h "$DEST/db.sqlite3" | cut -f1))"
fi

# Fail loudly rather than shipping credentials on a USB stick.
if find "$DEST" -name '.env' -not -name '.env.example' | grep -q .; then
  echo
  echo "REFUSING TO BUILD: a .env file reached the package. Remove it and rebuild."
  exit 1
fi

echo
echo "Built: $DEST  ($(du -sh "$DEST" | cut -f1))"
echo "Copy that folder to the USB drive."
