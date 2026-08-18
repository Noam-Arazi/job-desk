"""Never show the human a job they already applied to.

The spec calls this suppression rather than filtering, and the difference is
worth keeping: a suppressed posting is not judged to be irrelevant, it is judged
to be finished. It reads on the store's applied-blocklist, which is keyed by
fingerprint and survives between runs — the whole reason the store exists.

It runs first in the chain because it is the only gate that can be certain
without reading a word of the posting.
"""

from __future__ import annotations

from collections.abc import Callable

from .result import GateResult, Verdict

GATE = "already_applied"


def check(*, fingerprint: str, has_applied: Callable[[str], bool] | None) -> GateResult:
    if has_applied is None or not fingerprint:
        return GateResult(GATE, Verdict.UNKNOWN, reason="no application history was consulted")
    if has_applied(fingerprint):
        return GateResult(
            GATE,
            Verdict.BLOCK,
            reason="already applied to this role",
            evidence=fingerprint,
        )
    return GateResult(GATE, Verdict.PASS, reason="not applied to")
