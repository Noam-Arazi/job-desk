#!/bin/sh
# The Chrome that LinkedIn and Jobify are read through — Noam's own sign-ins,
# in a profile of its own. Chrome refuses a debugging port on the default
# profile (since 136), so this is a second profile that lives next to the
# everyday one: Noam signs in to LinkedIn and Jobify here once and leaves the
# window open. The morning pass attaches to it on the port below; when the
# window is closed the pass falls back to the logged-out paths and says so.
#
# Idempotent: a second call while the window is up does nothing.

PORT="${DESK_CHROME_PORT:-9222}"
PROFILE="${DESK_CHROME_PROFILE:-$HOME/.job-desk-chrome}"
CHROME="/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"

if curl -s "http://127.0.0.1:$PORT/json/version" >/dev/null 2>&1; then
    echo "job-desk chrome already up on $PORT"
    exit 0
fi

mkdir -p "$PROFILE"
"$CHROME" \
    --user-data-dir="$PROFILE" \
    --remote-debugging-port="$PORT" \
    --remote-debugging-address=127.0.0.1 \
    --no-first-run \
    --no-default-browser-check \
    "https://www.linkedin.com/login" "https://www.jobify360.co.il/" \
    >/dev/null 2>&1 &

echo "job-desk chrome started on $PORT, profile $PROFILE"
