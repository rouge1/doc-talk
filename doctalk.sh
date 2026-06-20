#!/bin/bash
#
# doctalk.sh — start/stop/restart/status for the local doctalk stack.
#
# Three long-running processes (the website is the React SPA, backed by the API):
#   • api      — FastAPI/uvicorn backend on :8000  (`doctalk serve`)
#                serves the JSON API (/api, consumed by the SPA) + legacy Jinja pages.
#   • web      — the Vite/React website on :5173    (`npm run dev` in frontend/)
#                THIS is the site you open; it proxies /api → the api backend.
#   • watcher  — polls the inbox/ drop dir and auto-ingests new files
#                (`doctalk ingest <file>`; re-drops are idempotent no-ops).
#
# Truth store: SQLite dev.db for now (MySQL comes later). On `start` the schema
# is brought up with `alembic upgrade head` (creates dev.db if missing).
#
# Usage: ./doctalk.sh {start|stop|restart|status}

# --- Locations ---------------------------------------------------------------
SCRIPT_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"
PROJECT_DIR="$SCRIPT_DIR"
FRONTEND_DIR="$PROJECT_DIR/frontend"
RUN_DIR="$PROJECT_DIR/run"
mkdir -p "$RUN_DIR"

API_PID_FILE="$RUN_DIR/api.pid"
WEB_PID_FILE="$RUN_DIR/web.pid"
WATCHER_PID_FILE="$RUN_DIR/watcher.pid"
API_LOG="$RUN_DIR/api.log"
WEB_LOG="$RUN_DIR/web.log"
WATCHER_LOG="$RUN_DIR/watcher.log"
WATCHER_STATE="$RUN_DIR/watcher.state"   # files already ingested (mtime+size+path)

# --- Config (env-overridable) ------------------------------------------------
# SQLite truth store until MySQL is wired up. Absolute path so it resolves the
# same no matter where the script is invoked from.
export DOCTALK_DB_URL="${DOCTALK_DB_URL:-sqlite:///$PROJECT_DIR/dev.db}"

CONDA_ENV="doctalk"                                   # the project env (see CLAUDE.md)
API_HOST="${DT_API_HOST:-127.0.0.1}"                  # FastAPI backend bind address
API_PORT="${DT_API_PORT:-8000}"                       # FastAPI backend port (Vite proxy target)
WEB_HOST="${DT_WEB_HOST:-127.0.0.1}"                  # Vite dev server bind address
WEB_PORT="${DT_WEB_PORT:-5173}"                       # Vite dev server port (the website)
INBOX="${DOCTALK_WATCHED_DIR:-$PROJECT_DIR/inbox}"    # drop files here
WATCH_INTERVAL="${DT_WATCH_INTERVAL:-5}"              # inbox poll seconds
WATCH_SETTLE="${DT_WATCH_SETTLE:-3}"                  # min file age before ingest (still-copying guard)
OLLAMA_HOST="${DOCTALK_OLLAMA_HOST:-http://127.0.0.1:11434}"  # local LLM/VLM server (config.py default)

# -----------------------------------------------------------------------------
ensure_conda_env() {
    if ! command -v conda &> /dev/null; then
        echo "WARNING: conda command not found. Make sure conda is installed and in PATH."
        return 1
    fi

    if [ "${CONDA_DEFAULT_ENV:-}" = "$CONDA_ENV" ]; then
        echo "✓ Conda environment '$CONDA_ENV' is active"
    else
        local base
        base=$(conda info --base 2>/dev/null)
        if [ -z "$base" ]; then
            echo "ERROR: Could not determine conda base directory"
            return 1
        fi
        # shellcheck disable=SC1091
        source "$base/etc/profile.d/conda.sh"
        echo "Activating conda environment '$CONDA_ENV'..."
        if ! conda activate "$CONDA_ENV" 2>/dev/null; then
            echo "ERROR: Could not activate conda env '$CONDA_ENV'."
            echo "       Try: conda activate $CONDA_ENV"
            return 1
        fi
        echo "✓ Activated conda environment '$CONDA_ENV'"
    fi

    if ! command -v doctalk &> /dev/null; then
        echo "ERROR: 'doctalk' not on PATH in env '$CONDA_ENV'. Run: pip install -e \".[dev]\""
        return 1
    fi
    return 0
}

# Non-blocking: chat, synthesis, and VLM/OCR ingest stages need Ollama, but the
# web UI and plain-text ingest boot without it. Warn, never abort.
check_ollama() {
    if curl -fsS --max-time 2 "$OLLAMA_HOST/api/tags" >/dev/null 2>&1; then
        echo "✓ Ollama reachable at $OLLAMA_HOST"
    else
        echo "WARNING: Ollama not reachable at $OLLAMA_HOST"
        echo "         Chat / synthesis / VLM ingest won't work until it's up — start it with: ollama serve"
        echo "         (override the address with DOCTALK_OLLAMA_HOST)"
    fi
}

ensure_db() {
    cd "$PROJECT_DIR" || return 1
    echo "Bringing schema up to head (alembic) → $DOCTALK_DB_URL"
    if ! alembic upgrade head; then
        echo "ERROR: 'alembic upgrade head' failed."
        return 1
    fi
    return 0
}

# Frontend deps must exist before the Vite dev server can boot. Auto-install on
# first run; warn (and let `start` skip web) if npm is missing entirely.
ensure_frontend_deps() {
    if ! command -v npm &> /dev/null; then
        echo "WARNING: npm not found — cannot start the website (Vite). Install Node.js to enable it."
        return 1
    fi
    if [ ! -d "$FRONTEND_DIR/node_modules" ]; then
        echo "Installing frontend deps (first run: npm install)..."
        ( cd "$FRONTEND_DIR" && npm install ) || { echo "ERROR: npm install failed."; return 1; }
    fi
    return 0
}

# --- Watcher: poll the inbox and ingest new/changed files --------------------
# Run in the background (see start_watcher). Tracks what it has ingested in
# WATCHER_STATE keyed by mtime+size+path, so steady-state cycles don't re-hash
# unchanged files; a changed file gets a new key and is re-ingested (and doctalk
# dedups by content_hash, so identical content is still a no-op).
watch_loop() {
    mkdir -p "$INBOX"
    touch "$WATCHER_STATE"
    echo "[watcher] watching $INBOX every ${WATCH_INTERVAL}s (settle ${WATCH_SETTLE}s)"
    while true; do
        local now; now=$(date +%s)
        while IFS= read -r -d '' f; do
            local mtime size age key
            mtime=$(stat -c '%Y' "$f" 2>/dev/null) || continue
            size=$(stat -c '%s' "$f" 2>/dev/null) || continue
            age=$(( now - mtime ))
            # Skip files still being written (mtime too recent); catch them next cycle.
            [ "$age" -lt "$WATCH_SETTLE" ] && continue
            key="$mtime $size $f"
            grep -qxF "$key" "$WATCHER_STATE" 2>/dev/null && continue
            echo "[watcher] $(date '+%F %T') ingest: $f"
            if doctalk ingest "$f"; then
                echo "$key" >> "$WATCHER_STATE"
            else
                echo "[watcher] ingest FAILED (will retry next cycle): $f"
            fi
        done < <(find "$INBOX" -type f ! -name '.*' -print0 2>/dev/null)
        sleep "$WATCH_INTERVAL"
    done
}

start_api() {
    if [ -f "$API_PID_FILE" ] && kill -0 "$(cat "$API_PID_FILE")" 2>/dev/null; then
        echo "API already running (PID: $(cat "$API_PID_FILE"))"
        return 1
    fi
    cd "$PROJECT_DIR" || return 1
    doctalk serve --host "$API_HOST" --port "$API_PORT" >> "$API_LOG" 2>&1 &
    disown $!
    echo $! > "$API_PID_FILE"
    echo "API (backend) started (PID: $!)  → $API_LOG"
    echo "   http://$API_HOST:$API_PORT  (JSON API + legacy Jinja UI)"
}

start_web() {
    if [ -f "$WEB_PID_FILE" ] && kill -0 "$(cat "$WEB_PID_FILE")" 2>/dev/null; then
        echo "Website already running (PID: $(cat "$WEB_PID_FILE"))"
        return 1
    fi
    ensure_frontend_deps || return 1
    cd "$FRONTEND_DIR" || return 1
    npm run dev -- --host "$WEB_HOST" --port "$WEB_PORT" >> "$WEB_LOG" 2>&1 &
    disown $!
    echo $! > "$WEB_PID_FILE"
    echo "Website (Vite) started (PID: $!)  → $WEB_LOG"
    echo "   http://$WEB_HOST:$WEB_PORT  ← open this"
}

start_watcher() {
    if [ -f "$WATCHER_PID_FILE" ] && kill -0 "$(cat "$WATCHER_PID_FILE")" 2>/dev/null; then
        echo "Watcher already running (PID: $(cat "$WATCHER_PID_FILE"))"
        return 1
    fi
    cd "$PROJECT_DIR" || return 1
    mkdir -p "$INBOX"
    watch_loop >> "$WATCHER_LOG" 2>&1 &
    disown $!
    echo $! > "$WATCHER_PID_FILE"
    echo "Watcher started (PID: $!)  → $WATCHER_LOG"
    echo "   inbox: $INBOX"
}

# Kill a process and all its descendants (npm → sh → vite, watcher → sleep/ingest).
_kill_tree() {
    local pid="$1" child
    for child in $(pgrep -P "$pid" 2>/dev/null); do
        _kill_tree "$child"
    done
    kill "$pid" 2>/dev/null
}

_stop() {  # _stop <name> <pidfile>
    local name="$1" pidfile="$2"
    if [ -f "$pidfile" ]; then
        local pid; pid=$(cat "$pidfile")
        if kill -0 "$pid" 2>/dev/null; then
            _kill_tree "$pid"
            echo "$name stopped (PID: $pid)"
        else
            echo "$name not running (stale PID file)"
        fi
        rm -f "$pidfile"
    else
        echo "$name not running"
    fi
}

_status() {  # _status <name> <pidfile> <extra>
    local name="$1" pidfile="$2" extra="$3"
    echo "=== $name ==="
    if [ -f "$pidfile" ] && kill -0 "$(cat "$pidfile")" 2>/dev/null; then
        echo "  Status: RUNNING (PID: $(cat "$pidfile"))  $extra"
    else
        echo "  Status: STOPPED"
        [ -f "$pidfile" ] && rm -f "$pidfile"
    fi
}

start_all() {
    ensure_conda_env || { echo; echo "Failed to ensure conda environment. Aborting."; exit 1; }
    ensure_db        || { echo; echo "Failed to ensure database schema. Aborting.";   exit 1; }
    check_ollama
    echo
    start_api       # backend first — the website proxies /api to it
    start_web
    start_watcher
    echo
    echo "Logs:  run/api.log  run/web.log  run/watcher.log"
    echo "Open:  http://$WEB_HOST:$WEB_PORT"
}

case "$1" in
    start)
        start_all
        ;;
    stop)
        _stop "Website" "$WEB_PID_FILE"
        _stop "API"     "$API_PID_FILE"
        _stop "Watcher" "$WATCHER_PID_FILE"
        ;;
    restart)
        _stop "Website" "$WEB_PID_FILE"
        _stop "API"     "$API_PID_FILE"
        _stop "Watcher" "$WATCHER_PID_FILE"
        sleep 2   # let the servers release their ports
        echo
        start_all
        ;;
    status)
        _status "Website (Vite SPA)"      "$WEB_PID_FILE"     "http://$WEB_HOST:$WEB_PORT"
        echo
        _status "API (doctalk serve)"     "$API_PID_FILE"     "http://$API_HOST:$API_PORT"
        echo
        _status "Watcher (inbox ingest)"  "$WATCHER_PID_FILE" "inbox: $INBOX"
        ;;
    *)
        echo "Usage: $0 {start|stop|restart|status}"
        exit 1
        ;;
esac
