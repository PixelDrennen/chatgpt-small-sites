#!/usr/bin/env bash
# Starts the local server and opens the correct HTTP address. Do not open index.html directly.
set -euo pipefail
cd "$(dirname "$(readlink -f "$0")")"

if ! command -v python3 >/dev/null; then
  echo "Python 3 is required but was not found."
  read -rp "Press Enter to close..."
  exit 1
fi

# A previous monitor owns this file only while it is running. Stop it so a
# freshly extracted build cannot silently leave the browser on old code.
if [[ -f .p4-monitor.pid ]]; then
  old_pid="$(<.p4-monitor.pid)"
  if [[ "$old_pid" =~ ^[0-9]+$ ]] && kill -0 "$old_pid" 2>/dev/null; then
    echo "Stopping the previous monitor instance..."
    kill "$old_pid" 2>/dev/null || true
    for _ in {1..20}; do kill -0 "$old_pid" 2>/dev/null || break; sleep 0.1; done
  fi
  rm -f .p4-monitor.pid
fi

python3 app.py &
server_pid=$!
sleep 0.4
if ! kill -0 "$server_pid" 2>/dev/null; then
  echo "Could not start the monitor. Port 8765 may still be held by an older copy."
  echo "Stop the older monitor terminal with Ctrl+C, then run this launcher again."
  exit 1
fi
xdg-open http://127.0.0.1:8765 >/dev/null 2>&1 || true
echo "Perforce Workspace Monitor is running at http://127.0.0.1:8765"
echo "Close this terminal or press Ctrl+C to stop it."
trap 'kill "$server_pid" 2>/dev/null || true' EXIT INT TERM
wait "$server_pid"
