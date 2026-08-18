"""The submission manager — everything after a posting has been judged.

Four things live here, and the boundary between them is the point:

    states      which moves the pipeline allows, as data derived from the spec
    timers      the follow-up and staleness windows, as arithmetic on dates
    digest      the daily ranked shortlist, assembled from what is stored
    delivery    stdout by default, Telegram only when switched on and configured

The system never applies and never sends anything on Noam's behalf. That is
stated in spec/search.yaml as `manager.delivery.auto_apply: never`, so the
answer lives in the specification rather than only in the code, and
`delivery.check_auto_apply` stops the run if the spec is ever edited to say
otherwise. Approving a posting is a human act that gets recorded; there is no
code path from a recorded approval to a submission.
"""

from __future__ import annotations

from .delivery import DeliveryError, Sink, StdoutSink, TelegramSink, check_auto_apply, sink_for
from .digest import Digest, Item, build

# `render` itself is deliberately not re-exported: the name would shadow the
# submodule of the same name, and `manager.render.as_text` is how it reads.
from .render import as_json, as_telegram, as_text
from .states import IllegalTransition, Move, UnknownState, move, transitions
from .timers import Nudge, due, due_at_for, sweep

__all__ = [
    "DeliveryError",
    "Digest",
    "IllegalTransition",
    "Item",
    "Move",
    "Nudge",
    "Sink",
    "StdoutSink",
    "TelegramSink",
    "UnknownState",
    "as_json",
    "as_telegram",
    "as_text",
    "build",
    "check_auto_apply",
    "due",
    "due_at_for",
    "move",
    "sink_for",
    "sweep",
    "transitions",
]
