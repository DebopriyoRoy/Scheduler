#!/bin/bash
#
# Start the Spirit Scheduling Engine and open it in your browser.
# Closing this window stops the application.

set -uo pipefail

TARGET="$HOME/SpiritScheduler"
PORT=8765
URL="http://127.0.0.1:$PORT/"

bold() { printf "\033[1m%s\033[0m\n" "$1"; }

clear 2>/dev/null || true
bold "Spirit Scheduling Engine"
echo

if [ ! -d "$TARGET/.venv" ]; then
  printf "\033[31mNot installed yet.\033[0m Run INSTALL.command from the USB drive first.\n\n"
  echo "Press any key to close."
  read -r -n 1
  exit 1
fi

cd "$TARGET"

# Refuse to start twice: a second server on the same database invites conflicting writes.
if lsof -nP -iTCP:"$PORT" -sTCP:LISTEN >/dev/null 2>&1; then
  echo "Already running. Opening it in your browser."
  open "$URL"
  echo
  if [ -t 0 ]; then echo "Press any key to close this window."; read -r -n 1; fi
  exit 0
fi

echo "Starting…"
"$TARGET/.venv/bin/python" manage.py runserver "$PORT" --noreload &
SERVER_PID=$!

# Stop the server when this window closes, rather than leaving it orphaned.
cleanup() {
  echo
  echo "Stopping…"
  kill "$SERVER_PID" 2>/dev/null || true
  wait "$SERVER_PID" 2>/dev/null || true
  echo "Stopped."
}
trap cleanup EXIT INT TERM

for _ in $(seq 1 40); do
  if lsof -nP -iTCP:"$PORT" -sTCP:LISTEN >/dev/null 2>&1; then
    break
  fi
  sleep 0.25
done

if lsof -nP -iTCP:"$PORT" -sTCP:LISTEN >/dev/null 2>&1; then
  open "$URL"
  echo
  bold "Running at $URL"
  echo
  echo "  Leave this window open while you use the application."
  echo "  Close it, or press Ctrl-C, to stop."
  echo
else
  printf "\033[31mThe application did not start.\033[0m Any error appears above.\n"
fi

wait "$SERVER_PID"
