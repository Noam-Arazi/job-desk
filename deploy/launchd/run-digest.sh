#!/bin/sh
# The wrapper the launchd job runs. Its only reason to exist is the timeout.
#
# launchd has no key that bounds how long a job may run. ExitTimeOut is the
# grace period between SIGTERM and SIGKILL once launchd is already stopping the
# job; it does nothing for a job that is simply hung. macOS ships no timeout(1)
# either. So the ceiling is enforced here, by a watchdog subshell, and the value
# comes from the plist, which takes it from spec/search.yaml.
#
# Unset is a failure and not a default. A default here would be a second copy of
# a number that belongs in the spec, and the whole point of the ceiling is that
# it is stated on purpose.

set -u

if [ -z "${DESK_TIMEOUT_SECONDS:-}" ]; then
    echo "DESK_TIMEOUT_SECONDS is not set. It comes from the plist, which takes it" >&2
    echo "from spec/search.yaml digest.schedule.timeout_seconds. Refusing to run" >&2
    echo "unsupervised without an explicit ceiling." >&2
    exit 78
fi

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

# --send, since 19.08.2026. Telegram is on in the spec and the credentials are
# in .env, which `desk` now reads for itself — launchd cannot export them, and a
# scheduled run that quietly lost its channel is the exact failure delivery.py
# refuses. Into a disabled or unconfigured channel this is a hard error, never a
# silent print, so a broken token fails on day one instead of looking like a
# week of empty mornings.
"$UV" run --directory "$DESK_HOME" desk digest --format telegram --send &
job=$!

(
    sleep "$DESK_TIMEOUT_SECONDS"
    kill -TERM "$job" 2>/dev/null && {
        echo "digest exceeded ${DESK_TIMEOUT_SECONDS}s and was stopped" >&2
        sleep 10
        kill -KILL "$job" 2>/dev/null
    }
) &
watchdog=$!

wait "$job"
status=$?
kill "$watchdog" 2>/dev/null
exit "$status"
