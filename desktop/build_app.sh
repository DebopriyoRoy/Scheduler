#!/bin/bash
#
# Build "Spirit Scheduler.app" and wrap it in a .dmg for installation on another Mac.
#
#   ./desktop/build_app.sh
#
# Output: dist/Spirit Scheduler.dmg
#
# Note: the result runs only on the same processor architecture it was built on.
# PyInstaller cannot cross-compile, so an Apple Silicon build will not launch on an
# Intel Mac and vice versa.

set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PY="$REPO/.venv/bin/python"
ARCH="$(uname -m)"

cd "$REPO"

echo "Building Spirit Scheduler.app  (architecture: $ARCH)"
echo

echo "1. Collecting interface files"
SPIRIT_STATIC_ROOT="" DJANGO_DEBUG=true "$PY" manage.py collectstatic --noinput >/dev/null
echo "   done"

echo "2. Freezing application (several minutes)"
mkdir -p build; rm -rf build "dist/Spirit Scheduler.app" "dist/Spirit Scheduler"
"$PY" -m PyInstaller --noconfirm --clean --distpath dist --workpath build \
  desktop/SpiritScheduler.spec > /tmp/pyinstaller.log 2>&1 || {
    echo "   FAILED - last 25 lines:"; tail -25 /tmp/pyinstaller.log; exit 1;
  }
echo "   done ($(du -sh "dist/Spirit Scheduler.app" | cut -f1))"

echo "3. Building disk image"
DMG="dist/Spirit Scheduler.dmg"
rm -f "$DMG"
STAGE="$(mktemp -d)"
cp -R "dist/Spirit Scheduler.app" "$STAGE/"
ln -s /Applications "$STAGE/Applications"
cat > "$STAGE/READ ME.txt" <<'EOF'
SPIRIT SCHEDULER
================

TO INSTALL
  Drag "Spirit Scheduler" onto the Applications folder shown here.

TO RUN
  Open Applications and double-click Spirit Scheduler.
  Your browser opens automatically.

  The FIRST time only, macOS will say the app is from an unidentified
  developer. Right-click the app, choose Open, then choose Open again.
  This happens once.

TO SIGN IN
  Username: manager
  Password: spirit

  Change this after the first sign-in.

YOUR DATA
  Everything is stored in your home folder under
  Library/Application Support/Spirit Scheduler

  Reinstalling or updating the app never touches it.
EOF

hdiutil create -volname "Spirit Scheduler" -srcfolder "$STAGE" -ov -format UDZO "$DMG" >/dev/null
rm -rf "$STAGE"

echo "   done ($(du -sh "$DMG" | cut -f1))"
echo
echo "Built: $REPO/$DMG"
echo "Copy that single file to the office Mac and open it."
