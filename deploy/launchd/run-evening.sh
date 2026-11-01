#!/bin/sh
# The evening catch-up passes, added 01.11.2026 at Noam's request: at 17:00
# and 22:30, analyse the unanalysed backlog until the subscription's usage
# window runs out, then send the digest. No fetch and no tailoring — the
# morning pass does those. Jobify and GotFriends go first (priority_sites).
#
# "Until the usage runs out" is how the analyst stops: three postings failing
# in a row halts it (CONSECUTIVE_FAILURES), which is what a spent window looks
# like from here. The call ceiling below is a backstop, not the plan.
#
# DESK_RUN_ON limits the job to one date. The plist fires every 1 November;
# without the guard it would wake up again next year and spend.

set -u

[ -n "${DESK_TIMEOUT_SECONDS:-}" ]    || { echo "DESK_TIMEOUT_SECONDS is not set" >&2; exit 78; }
[ -n "${DESK_ENGINE:-}" ]             || { echo "DESK_ENGINE is not set" >&2; exit 78; }
[ -n "${DESK_ANALYZE_BUDGET_USD:-}" ] || { echo "DESK_ANALYZE_BUDGET_USD is not set" >&2; exit 78; }
[ -n "${DESK_ANALYZE_LIMIT:-}" ]      || { echo "DESK_ANALYZE_LIMIT is not set" >&2; exit 78; }

if [ -n "${DESK_RUN_ON:-}" ] && [ "$(date +%F)" != "$DESK_RUN_ON" ]; then
    echo "$(date '+%F %T') evening pass is for $DESK_RUN_ON only; nothing to do"
    exit 0
fi

DESK_HOME="${DESK_HOME:-$(cd "$(dirname "$0")/../.." && pwd)}"
export DESK_HOME
cd "$DESK_HOME" || exit 1

UV="$(command -v uv || true)"
for candidate in /opt/homebrew/bin/uv /usr/local/bin/uv "$HOME/.local/bin/uv"; do
    [ -n "$UV" ] && break
    [ -x "$candidate" ] && UV="$candidate"
done
[ -n "$UV" ] || { echo "uv not found on PATH" >&2; exit 127; }

echo "$(date '+%F %T') evening pass starting"

# Job control, so the pass runs in its own process group and the watchdog
# can stop all of it, as in run-digest.sh.
set -m

(
    status=0
    "$UV" run --directory "$DESK_HOME" desk analyze \
        --engine "$DESK_ENGINE" \
        --budget "$DESK_ANALYZE_BUDGET_USD" \
        --max-calls "$DESK_ANALYZE_LIMIT" \
        --write || status=1
    "$UV" run --directory "$DESK_HOME" desk digest --format telegram --send || exit 1
    exit "$status"
) &
job=$!

(
    sleep "$DESK_TIMEOUT_SECONDS"
    kill -TERM -"$job" 2>/dev/null && {
        echo "the evening pass exceeded ${DESK_TIMEOUT_SECONDS}s and was stopped" >&2
        sleep 10
        kill -KILL -"$job" 2>/dev/null
    }
) &
watchdog=$!

wait "$job"
status=$?
kill "$watchdog" 2>/dev/null
echo "$(date '+%F %T') evening pass finished, status $status"
exit "$status"
