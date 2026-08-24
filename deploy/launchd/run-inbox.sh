#!/bin/sh
# The other half of the daily loop: the digest goes out at 06:00, and this is
# what notices that Noam pressed something on it.
#
# It polls once and exits, every DESK_INBOX_INTERVAL seconds. Not a resident
# listener, for the same reason nothing else here is one: this is a laptop that
# sleeps, and a process that is not running is not listening. Telegram holds an
# unacknowledged press for 24 hours and hands it back until the cursor moves
# past it, so a poll that never happens loses nothing — the presses are still
# waiting the next time the machine is awake.
#
# The cost of the interval is how long Noam waits for the CV he just asked for,
# and nothing else: a poll that finds no press makes one HTTP request and exits
# in under a second.
#
# Unset is a failure, not a default — the same rule the morning pass runs under,
# and `--engine replay` is the same dangerous default it is there. A replayed
# tailoring returns a recorded document that looks exactly like a real one.

set -u

fail_unset() {
    echo "$1 is not set. It comes from the plist. Refusing to run unsupervised" >&2
    echo "without an explicit value; see the comment at the top of this script." >&2
    exit 78
}

[ -n "${DESK_ENGINE:-}" ]              || fail_unset DESK_ENGINE
[ -n "${DESK_INBOX_BUDGET_USD:-}" ]    || fail_unset DESK_INBOX_BUDGET_USD
[ -n "${DESK_INBOX_TIMEOUT_SECONDS:-}" ] || fail_unset DESK_INBOX_TIMEOUT_SECONDS

DESK_HOME="${DESK_HOME:-$(cd "$(dirname "$0")/../.." && pwd)}"
export DESK_HOME
cd "$DESK_HOME" || exit 1

UV="$(command -v uv || true)"
for candidate in /opt/homebrew/bin/uv /usr/local/bin/uv "$HOME/.local/bin/uv"; do
    [ -n "$UV" ] && break
    [ -x "$candidate" ] && UV="$candidate"
done
if [ -z "$UV" ]; then
    echo "uv not found on PATH. launchd starts with a minimal PATH; set it in the plist." >&2
    exit 127
fi

set -m
"$UV" run --directory "$DESK_HOME" desk inbox \
    --engine "$DESK_ENGINE" \
    --budget "$DESK_INBOX_BUDGET_USD" &
job=$!

# The same watchdog as the morning pass, and it matters more here rather than
# less: this runs unattended every few minutes, so one hung poll that is never
# stopped is a process left behind every interval until the machine is rebooted.
(
    sleep "$DESK_INBOX_TIMEOUT_SECONDS"
    kill -TERM -"$job" 2>/dev/null && {
        echo "the inbox poll exceeded ${DESK_INBOX_TIMEOUT_SECONDS}s and was stopped" >&2
        sleep 5
        kill -KILL -"$job" 2>/dev/null
    }
) &
watchdog=$!

wait "$job"
status=$?
kill "$watchdog" 2>/dev/null
exit "$status"
