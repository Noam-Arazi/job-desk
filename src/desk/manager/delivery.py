"""Where the digest goes — stdout by default, Telegram only when asked twice.

Delivery is the one place in this system that touches the outside world, so it
is built to be boring and to fail loudly.

    stdout is the default and it is a real sink, not a fallback. The digest is
    useful printed. Telegram is a convenience on top, and the system has to be
    fully usable with it switched off, because that is the state it ships in:
    `manager.delivery.telegram: false`.

    the credentials live in the environment and nowhere else. Not in the repo,
    not in a config file next to the spec, not in a default argument. A bot
    token in a file is a token in git history the week somebody forgets, and
    this repository is meant to be public.

    the token is never written anywhere. `__repr__` redacts it, so it cannot
    reach a traceback, a trace line or a debug print by accident. The only
    place it exists in this process is one attribute, and the only thing that
    reads that attribute is the URL builder inside `send`.

    `--send` into a disabled or unconfigured channel is an error, never a
    silent print. The failure mode being designed against is specific: Noam
    schedules the daily job with `--send`, the token is missing, the digest
    prints to a log file nobody opens, and he concludes for a week that there
    were no jobs. A crash on day one is strictly better.

And the rule that overrides everything else in this file: nothing here applies
to anything. `manager.delivery.auto_apply: never` is in the spec so that the
answer lives in the specification rather than only in the code, and
`check_auto_apply` refuses to run if the spec is ever edited to say otherwise.
Sending Noam a message is delivery. Sending an employer a message is not
something this system does.
"""

from __future__ import annotations

import os
import sys
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Protocol, TextIO

TOKEN_ENV = "DESK_TELEGRAM_BOT_TOKEN"
CHAT_ENV = "DESK_TELEGRAM_CHAT_ID"

NEVER = "never"

REDACTED = "<redacted>"


class DeliveryError(RuntimeError):
    """Delivery was asked for and cannot be done. Never downgraded to a print."""


class NeverApplies(RuntimeError):
    """The spec was edited to permit applying. The run stops instead."""


class Sink(Protocol):
    """One method, because delivery is one act and should not grow a workflow."""

    def send(self, text: str) -> None: ...


@dataclass
class StdoutSink:
    """The default. Prints the digest and nothing else."""

    stream: TextIO = field(default_factory=lambda: sys.stdout)
    sent: int = 0

    def send(self, text: str) -> None:
        self.stream.write(text if text.endswith("\n") else text + "\n")
        self.sent += 1


class TelegramSink:
    """One HTTP POST to the Bot API, built only when explicitly configured.

    `urllib` rather than a client library on purpose: this is the only network
    call the package makes, and a dependency that exists for one POST is a
    dependency that has to be installed to run the tests, which do not make it.
    """

    def __init__(self, token: str, chat_id: str) -> None:
        if not token or not chat_id:
            raise DeliveryError(f"telegram needs both {TOKEN_ENV} and {CHAT_ENV}")
        self._token = token
        self.chat_id = chat_id
        self.sent = 0

    def __repr__(self) -> str:
        """Redacted, so a traceback or a log line cannot carry the token."""
        return f"TelegramSink(chat_id={self.chat_id!r}, token={REDACTED})"

    __str__ = __repr__

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> TelegramSink:
        source = os.environ if env is None else env
        token = str(source.get(TOKEN_ENV, "") or "")
        chat_id = str(source.get(CHAT_ENV, "") or "")
        missing = [name for name, value in ((TOKEN_ENV, token), (CHAT_ENV, chat_id)) if not value]
        if missing:
            raise DeliveryError(
                "telegram delivery is on in the spec but the environment is not set: "
                + ", ".join(missing)
            )
        return cls(token, chat_id)

    def send(self, text: str) -> None:
        import json
        import urllib.request

        payload = json.dumps({"chat_id": self.chat_id, "text": text}).encode("utf-8")
        request = urllib.request.Request(
            f"https://api.telegram.org/bot{self._token}/sendMessage",
            data=payload,
            headers={"Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                if response.status >= 400:
                    raise DeliveryError(f"telegram refused the message: HTTP {response.status}")
        except DeliveryError:
            raise
        except Exception as error:  # noqa: BLE001 - the token must not reach the traceback
            raise DeliveryError(f"telegram delivery failed: {type(error).__name__}") from None
        self.sent += 1


def telegram_enabled(spec: Mapping[str, Any]) -> bool:
    delivery = (spec.get("manager") or {}).get("delivery") or {}
    return bool(delivery.get("telegram", False))


def check_auto_apply(spec: Mapping[str, Any]) -> None:
    """Refuse to run if the spec no longer says the system never applies.

    This is the guard rail that is enforced in code rather than in a prompt. It
    exists so that changing the sentence in the spec is not enough to make the
    system apply for anybody — there is no code path that would, and this stops
    the run rather than letting the edit look like it worked.
    """
    delivery = (spec.get("manager") or {}).get("delivery") or {}
    stated = str(delivery.get("auto_apply", NEVER))
    if stated != NEVER:
        raise NeverApplies(
            f"manager.delivery.auto_apply is {stated!r}. This system does not apply "
            "on anyone's behalf, and there is no code path that would."
        )


def sink_for(
    spec: Mapping[str, Any],
    *,
    send: bool,
    env: Mapping[str, str] | None = None,
    stream: TextIO | None = None,
) -> Sink:
    """Pick the sink. `send` is the only thing that can select a network one."""
    if not send:
        return StdoutSink(stream=stream or sys.stdout)
    if not telegram_enabled(spec):
        raise DeliveryError(
            "--send was passed but manager.delivery.telegram is false in spec/search.yaml. "
            "Turn it on there first; nothing is printed instead."
        )
    return TelegramSink.from_env(env)
