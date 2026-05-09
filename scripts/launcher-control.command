#!/bin/zsh
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
APP_HOST="${EXTRACTOR_HOST:-127.0.0.1}"
PORT="${PORT:-5050}"
APP_URL="http://${APP_HOST}:${PORT}/"
RUNTIME_DIR="$HOME/Library/Application Support/YouTube Extractor"
PID_FILE="$RUNTIME_DIR/server.pid"
LOG_FILE="$RUNTIME_DIR/server.log"

mkdir -p "$RUNTIME_DIR"

find_python_bin() {
  local candidates=()
  local shell_python=""
  shell_python="$(command -v python3 2>/dev/null || true)"

  [[ -n "${PYTHON_BIN:-}" ]] && candidates+=("$PYTHON_BIN")
  [[ -n "$shell_python" ]] && candidates+=("$shell_python")
  candidates+=(
    "/Library/Frameworks/Python.framework/Versions/3.12/bin/python3"
    "/usr/local/bin/python3"
    "/opt/homebrew/bin/python3"
    "/usr/bin/python3"
  )

  local seen=""
  local candidate=""
  for candidate in "${candidates[@]}"; do
    [[ -n "$candidate" && -x "$candidate" ]] || continue
    if [[ " $seen " == *" $candidate "* ]]; then
      continue
    fi
    seen="$seen $candidate"
    if "$candidate" -c 'import flask, yt_dlp, youtube_transcript_api' >/dev/null 2>&1; then
      echo "$candidate"
      return 0
    fi
  done
  return 1
}

read_pid() {
  if [[ -f "$PID_FILE" ]]; then
    tr -d '[:space:]' < "$PID_FILE"
  fi
}

pid_is_running() {
  local pid="${1:-}"
  [[ -n "$pid" ]] && kill -0 "$pid" 2>/dev/null
}

pid_is_listening() {
  local pid="${1:-}"
  [[ -n "$pid" ]] && /usr/sbin/lsof -nP -a -p "$pid" -iTCP:"$PORT" -sTCP:LISTEN >/dev/null 2>&1
}

port_is_taken() {
  /usr/sbin/lsof -nP -iTCP:"$PORT" -sTCP:LISTEN >/dev/null 2>&1
}

cleanup_stale_pid() {
  local pid
  pid="$(read_pid)"
  if [[ -n "$pid" ]] && ! pid_is_running "$pid"; then
    rm -f "$PID_FILE"
  fi
}

start_server() {
  cleanup_stale_pid

  local pid
  pid="$(read_pid)"
  if pid_is_running "$pid" && pid_is_listening "$pid"; then
    return 0
  fi

  if port_is_taken; then
    echo "Port $PORT is already in use by another process." >&2
    exit 1
  fi

  local python_bin=""
  if ! python_bin="$(find_python_bin)"; then
    {
      echo "No suitable Python interpreter was found."
      echo "Tried common python3 locations, but none could import: flask, yt_dlp, youtube_transcript_api"
    } >>"$LOG_FILE"
    echo "No suitable Python interpreter was found. Check $LOG_FILE" >&2
    exit 1
  fi

  cd "$PROJECT_DIR"
  APP_DEBUG=0 HOST="$APP_HOST" PORT="$PORT" nohup "$python_bin" app.py >>"$LOG_FILE" 2>&1 &
  pid=$!
  echo "$pid" > "$PID_FILE"

  for _ in {1..50}; do
    if pid_is_running "$pid" && pid_is_listening "$pid"; then
      return 0
    fi
    sleep 0.2
  done

  echo "The local server did not start successfully. Check $LOG_FILE" >&2
  exit 1
}

stop_server() {
  local pid
  pid="$(read_pid)"
  if ! pid_is_running "$pid"; then
    rm -f "$PID_FILE"
    return 0
  fi

  kill "$pid" >/dev/null 2>&1 || true
  for _ in {1..25}; do
    if ! pid_is_running "$pid"; then
      rm -f "$PID_FILE"
      return 0
    fi
    sleep 0.2
  done

  kill -9 "$pid" >/dev/null 2>&1 || true
  rm -f "$PID_FILE"
}

open_browser() {
  if [[ "${SKIP_BROWSER_OPEN:-0}" == "1" ]]; then
    return 0
  fi
  if [[ -d "/Applications/Google Chrome.app" ]]; then
    /usr/bin/open -a "Google Chrome" "$APP_URL"
  else
    /usr/bin/open "$APP_URL"
  fi
}

status_server() {
  cleanup_stale_pid
  local pid
  pid="$(read_pid)"
  if pid_is_running "$pid" && pid_is_listening "$pid"; then
    echo "running:$pid"
  else
    echo "stopped"
  fi
}

case "${1:-}" in
  start)
    start_server
    ;;
  stop)
    stop_server
    ;;
  open)
    open_browser
    ;;
  status)
    status_server
    ;;
  *)
    echo "Usage: $0 {start|stop|open|status}" >&2
    exit 1
    ;;
esac
