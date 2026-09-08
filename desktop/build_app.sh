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

echo "3. Signing"
# Ad-hoc signature. This does not satisfy Gatekeeper on its own - only Apple
# notarization does, which needs a paid Developer account - but it makes the bundle
# internally consistent, so macOS reports it as "unidentified developer" rather than
# the far more alarming and unrecoverable "app is damaged".
codesign --force --deep --sign - --timestamp=none "dist/Spirit Scheduler.app" 2>/dev/null
codesign --verify --deep --strict "dist/Spirit Scheduler.app" 2>/dev/null \
  && echo "   signed (ad-hoc)" || echo "   WARNING: signature could not be verified"

echo "4. Building disk image"
DMG="dist/Spirit Scheduler.dmg"
rm -f "$DMG"
# Staged beside the build output rather than in mktemp's directory. hdiutil fails
# with "Resource busy" reading a source folder under /var/folders on this macOS -
# reproducibly, with nothing holding the files and no image mounted - so the disk
# image step failed while the .app beside it had built perfectly.
STAGE="$REPO/build/dmg-stage"
rm -rf "$STAGE"; mkdir -p "$STAGE"
cp -R "dist/Spirit Scheduler.app" "$STAGE/"
ln -s /Applications "$STAGE/Applications"
cp "$REPO/packaging/READ-ME-FIRST.txt" "$STAGE/READ ME FIRST.txt"

hdiutil create -volname "Spirit Scheduler" -srcfolder "$STAGE" -ov -format UDZO "$DMG" >/dev/null
rm -rf "$STAGE"

echo "   done ($(du -sh "$DMG" | cut -f1))"
echo
echo "Built: $REPO/$DMG"
echo "Copy that single file to the office Mac and open it."
