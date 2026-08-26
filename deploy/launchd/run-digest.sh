#!/bin/sh
# The wrapper the launchd job runs. It is the whole morning pass, in order:
# fetch, analyse, tailor, deliver. Before 23.08.2026 it ran only the last of the three,
# which meant a scheduled morning re-ranked a store nobody had refreshed and
# sent the same list every day. A digest is a view over the store; it fetches
# nothing and it judges nothing.
#
# launchd has no key that bounds how long a job may run. ExitTimeOut is the
# grace period between SIGTERM and SIGKILL once launchd is already stopping the
# job; it does nothing for a job that is simply hung. macOS ships no timeout(1)
# either. So the ceiling is enforced here, by a watchdog subshell, and the value
# comes from the plist, which takes it from spec/search.yaml.
#
# Unset is a failure and not a default, and that rule now covers four numbers,
# not one. A default here would be a second copy of a value that belongs in the
# plist or the spec — and for the two that govern spending, and for the engine,
# a default is worse than a duplicate. `--engine replay` against a live store
# returns recorded answers that look exactly like real judgements: the failure
# that reads as a valid result, which is the one thing this repo keeps refusing
# to ship.

set -u

fail_unset() {
    echo "$1 is not set. It comes from the plist. Refusing to run unsupervised" >&2
    echo "without an explicit value; see the comment at the top of this script." >&2
    exit 78
}

[ -n "${DESK_TIMEOUT_SECONDS:-}" ]    || fail_unset DESK_TIMEOUT_SECONDS
[ -n "${DESK_ENGINE:-}" ]             || fail_unset DESK_ENGINE
[ -n "${DESK_ANALYZE_BUDGET_USD:-}" ] || fail_unset DESK_ANALYZE_BUDGET_USD
[ -n "${DESK_ANALYZE_LIMIT:-}" ]      || fail_unset DESK_ANALYZE_LIMIT
[ -n "${DESK_ANALYZE_TIMEOUT_SECONDS:-}" ] || fail_unset DESK_ANALYZE_TIMEOUT_SECONDS
[ -n "${DESK_TAILOR_BUDGET_USD:-}" ]  || fail_unset DESK_TAILOR_BUDGET_USD
[ -n "${DESK_TAILOR_TIMEOUT_SECONDS:-}" ] || fail_unset DESK_TAILOR_TIMEOUT_SECONDS

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

# Which sites run is not a list kept here. The spec says what is enabled and the
# registry says what is built, and the morning pass is the intersection of the
# two. A site that is enabled with no module — jobify, as of today — is named on
# stdout rather than skipped in silence, because "enabled" in the spec is a
# promise a reader will otherwise believe.
SITES="$("$UV" run --directory "$DESK_HOME" python - <<'PY'
from desk.config import load_spec
from desk.sites import MODULES

enabled = [s["id"] for s in load_spec().get("sites", []) if s.get("enabled")]
print(" ".join(s for s in enabled if s in MODULES))
print(" ".join(s for s in enabled if s not in MODULES))
PY
)"
runnable="$(echo "$SITES" | sed -n 1p)"
unbuilt="$(echo "$SITES" | sed -n 2p)"

if [ -z "$runnable" ]; then
    echo "no enabled site has a module. Nothing to fetch; refusing to deliver a" >&2
    echo "digest over a store that was never refreshed." >&2
    exit 65
fi
echo "sites    $runnable"
[ -n "$unbuilt" ] && echo "unbuilt  $unbuilt   (enabled in the spec, no module yet)"

# Monitor mode, so the pipeline below becomes its own process group and the
# watchdog can signal the whole tree. Without it a TERM reaches the subshell
# and leaves the fetch that actually hung still running.
set -m

(
    status=0
    for site in $runnable; do
        # A board that is down, rate-limiting or newly re-laid-out must not cost
        # the morning its other four sources. It is recorded and the pass goes on.
        if ! "$UV" run --directory "$DESK_HOME" desk fetch --site "$site" --write; then
            echo "fetch $site failed; continuing with the rest" >&2
            status=1
        fi
    done

    # The analyst is the only step here that spends, so it carries a budget of
    # its own on top of the wall clock — and a clock of its own besides.
    #
    # The clock is the important one and it was learned the hard way. The outer
    # watchdog stops the whole pass, which means an analyst that overruns takes
    # the digest down with it and the morning arrives with no message at all.
    # That is the worst outcome available: no delivery reads exactly like a day
    # with no matches. The analyst stores each verdict as it is made, so a
    # stopped analyst keeps everything it judged before the signal and loses
    # only the posting in flight — and the digest ranks the whole store either
    # way. Better a shortlist one day stale than silence.
    #
    # It wrote in one transaction at the end until 26.08.2026, and the kill
    # below threw that transaction away every morning: the run spent its budget
    # and stored a single row on 24, 25 and 26 August.
    "$UV" run --directory "$DESK_HOME" desk analyze \
        --engine "$DESK_ENGINE" \
        --budget "$DESK_ANALYZE_BUDGET_USD" \
        --limit "$DESK_ANALYZE_LIMIT" \
        --write &
    analyst=$!
    (
        sleep "$DESK_ANALYZE_TIMEOUT_SECONDS"
        kill -TERM "$analyst" 2>/dev/null && {
            echo "analyst exceeded ${DESK_ANALYZE_TIMEOUT_SECONDS}s and was stopped;" >&2
            echo "delivering over the judgements already in the store" >&2
            sleep 5
            kill -KILL "$analyst" 2>/dev/null
        }
    ) &
    analyst_clock=$!
    wait "$analyst" || status=1
    kill "$analyst_clock" 2>/dev/null

    # Tailoring, since 24.08.2026, and it cuts for `approved` only — the
    # postings Noam marked on his phone, never the ones today happens to rank.
    # Tailoring the whole shortlist was built first and thrown away: it spends
    # a model call on four jobs out of five he would not have applied to.
    #
    # Most mornings this does nothing, and that is the intended shape. The
    # button he presses cuts the document there and then; this run is the
    # safety net for a decision made while the laptop was asleep, and for a
    # cut that failed and is worth one more try.
    #
    # Third clock in the pass, and the third instance of the same rule: it
    # gets its own so that slow tailoring costs its own work rather than the
    # delivery. A morning that arrives with one CV missing is a good morning.
    # A morning that arrives with nothing is indistinguishable from a morning
    # with no matches.
    "$UV" run --directory "$DESK_HOME" desk tailor --approved \
        --engine "$DESK_ENGINE" \
        --budget "$DESK_TAILOR_BUDGET_USD" \
        --write &
    cutter=$!
    (
        sleep "$DESK_TAILOR_TIMEOUT_SECONDS"
        kill -TERM "$cutter" 2>/dev/null && {
            echo "tailoring exceeded ${DESK_TAILOR_TIMEOUT_SECONDS}s and was stopped;" >&2
            echo "delivering with whatever documents are already on disk" >&2
            sleep 5
            kill -KILL "$cutter" 2>/dev/null
        }
    ) &
    cutter_clock=$!
    wait "$cutter" || {
        echo "tailoring failed or was stopped; continuing to the delivery" >&2
        status=1
    }
    kill "$cutter_clock" 2>/dev/null

    # --send, since 19.08.2026. Telegram is on in the spec and the credentials
    # are in .env, which `desk` reads for itself — launchd cannot export them,
    # and a scheduled run that quietly lost its channel is the exact failure
    # delivery.py refuses. Into a disabled or unconfigured channel this is a
    # hard error, never a silent print, so a broken token fails on day one
    # instead of looking like a week of empty mornings.
    "$UV" run --directory "$DESK_HOME" desk digest --format telegram --send || exit 1

    exit "$status"
) &
job=$!

(
    sleep "$DESK_TIMEOUT_SECONDS"
    kill -TERM -"$job" 2>/dev/null && {
        echo "the morning pass exceeded ${DESK_TIMEOUT_SECONDS}s and was stopped" >&2
        sleep 10
        kill -KILL -"$job" 2>/dev/null
    }
) &
watchdog=$!

wait "$job"
status=$?
kill "$watchdog" 2>/dev/null
exit "$status"
