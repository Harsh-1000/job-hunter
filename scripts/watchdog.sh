#!/usr/bin/env bash
# Server-level watchdog — runs from cron every 10 minutes (install: jobhunter watchdog install).
# Covers the case the bot can't report itself: container stopped/crashed/unhealthy or bot frozen.
#   * not running / unhealthy / heartbeat older than 5 min  -> `docker compose up -d` + ONE Telegram alert
#   * healthy again after an alert                           -> ONE "running again" message
set -uo pipefail
export PATH="$PATH:/usr/local/bin:/usr/bin:/bin"   # cron has a minimal PATH
DIR="${JOBHUNTER_DIR:-$(cd "$(dirname "$(readlink -f "$0")")/.." && pwd)}"
cd "$DIR" || exit 1
STATE="data/watchdog.alerted"
MAX_HEARTBEAT_AGE="${MAX_HEARTBEAT_AGE:-300}"

env_value() { grep -E "^$1=" .env 2>/dev/null | tail -1 | cut -d= -f2- | tr -d '\r'; }

admin_chats() {
    local ids; ids="$(env_value ADMIN_IDS | tr ',' ' ')"
    if [[ -z "${ids// }" && -f data/jobhunter.db ]]; then
        ids="$(python3 -c 'import sqlite3,sys; c=sqlite3.connect(sys.argv[1]);
print(" ".join(str(r[0]) for r in c.execute("SELECT chat_id FROM users WHERE is_admin=1")))' data/jobhunter.db 2>/dev/null)"
    fi
    echo "$ids"
}

notify() {
    local token; token="$(env_value TELEGRAM_BOT_TOKEN)"
    [[ -n "$token" ]] || return 0
    for chat in $(admin_chats); do
        curl -s --max-time 20 --form-string "chat_id=$chat" --form-string "text=$1" \
            "https://api.telegram.org/bot$token/sendMessage" >/dev/null || true
    done
}

status="$(docker inspect -f '{{.State.Status}} {{.State.Health.Status}}' job-hunter 2>/dev/null || echo "missing")"
if [[ -f data/heartbeat ]]; then
    hb_age=$(( $(date +%s) - $(stat -c %Y data/heartbeat) ))
else
    hb_age=999999
fi

problem=""
case "$status" in
    "running healthy"|"running starting") ;;
    *) problem="container is ${status}" ;;
esac
if (( hb_age > MAX_HEARTBEAT_AGE )); then
    problem="${problem:+$problem, }bot heartbeat is ${hb_age}s old"
fi

if [[ -n "$problem" ]]; then
    export UID GID="$(id -g)"
    docker compose up -d >/dev/null 2>&1 || true
    if [[ ! -f "$STATE" ]]; then
        notify "🔴 Job Hunter server watchdog: $problem. Restarting it now — I'll tell you when it's back."
        mkdir -p data && date -Is > "$STATE"
    fi
    echo "problem: $problem"
elif [[ -f "$STATE" ]]; then
    notify "✅ Job Hunter is running again (container healthy, bot responding)."
    rm -f "$STATE"
    echo "recovered"
else
    echo "ok"
fi
