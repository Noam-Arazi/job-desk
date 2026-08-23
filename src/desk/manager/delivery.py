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
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from hashlib import sha256
from pathlib import Path
from typing import Any, Protocol, TextIO

TOKEN_ENV = "DESK_TELEGRAM_BOT_TOKEN"
CHAT_ENV = "DESK_TELEGRAM_CHAT_ID"

NEVER = "never"

# Telegram's own ceilings, not policy knobs: a bot upload is capped at 50 MB
# and a caption at 1024 characters. A CV is three orders of magnitude under the
# first; the check is here so that an oversized file is named rather than
# turning into an opaque HTTP failure.
MAX_UPLOAD_BYTES = 50 * 1024 * 1024
CAPTION_LIMIT = 1024

REDACTED = "<redacted>"


class DeliveryError(RuntimeError):
    """Delivery was asked for and cannot be done. Never downgraded to a print."""


class NeverApplies(RuntimeError):
    """The spec was edited to permit applying. The run stops instead."""


@dataclass(frozen=True)
class Document:
    """One file to attach, and the caption that says which posting it is for.

    The caption is built by the caller and capped here. It follows the same
    one-direction-per-line rule as the digest itself: a Hebrew job title and an
    English score never share a line, because a phone reorders a mixed line
    exactly as a terminal does.
    """

    path: Path
    caption: str = ""

    def checked(self) -> Document:
        """Fail on a missing or oversized file here, before any upload starts.

        A tailored CV is written to disk by an earlier command, and between
        that write and this send a human may have moved it, renamed the folder
        it sits in, or opened it in Word. A file that is gone is reported by
        name — the digest promised a document and there is no document.
        """
        if not self.path.is_file():
            raise DeliveryError(f"attachment is not on disk: {self.path}")
        size = self.path.stat().st_size
        if size == 0:
            raise DeliveryError(f"attachment is empty: {self.path}")
        if size > MAX_UPLOAD_BYTES:
            raise DeliveryError(f"attachment is {size} bytes, over Telegram's cap: {self.path}")
        return self


class Sink(Protocol):
    """Two acts: the digest, and the documents it points at.

    This started as one method, on the reasoning that delivery is one act. That
    was right about the text and wrong about the phone. The digest names a
    tailored CV by its path on Noam's laptop, which is a fact he can act on at
    a terminal and cannot act on anywhere else — the message arrives on a phone
    and the document it refers to does not. Applying is done from wherever the
    posting is read, so the file has to travel with the message that ranked it.

    `send_documents` is separate from `send` rather than folded into it because
    the text is the digest and the attachments are a convenience on top: a
    channel that cannot carry files still delivers the shortlist.
    """

    def send(self, text: str) -> None: ...

    def send_documents(self, documents: Sequence[Document]) -> None: ...


@dataclass
class StdoutSink:
    """The default. Prints the digest and nothing else."""

    stream: TextIO = field(default_factory=lambda: sys.stdout)
    sent: int = 0
    attached: int = 0

    def send(self, text: str) -> None:
        self.stream.write(text if text.endswith("\n") else text + "\n")
        self.sent += 1

    def send_documents(self, documents: Sequence[Document]) -> None:
        """Names the files rather than copying them. A terminal has the disk.

        The check still runs, so a digest that points at a CV which is no
        longer there fails the same way on both channels. The failure being
        guarded against is a promised document that does not exist, and that is
        not a property of the transport.
        """
        for document in documents:
            document.checked()
            self.stream.write(f"attached  {document.path}\n")
        self.attached += len(documents)


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
        self.attached = 0

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

        payload = json.dumps({"chat_id": self.chat_id, "text": text}).encode("utf-8")
        self._post("sendMessage", payload, "application/json", what="the message")
        self.sent += 1

    def send_documents(self, documents: Sequence[Document]) -> None:
        """One upload per document, and the first failure stops the rest.

        Every file is checked before the first byte goes out, so a shortlist
        whose third CV was moved does not deliver two attachments and then
        fail — either the set is deliverable or the caller hears about it with
        nothing half-sent.
        """
        checked = [document.checked() for document in documents]
        for document in checked:
            body, content_type = _multipart(
                fields={
                    "chat_id": self.chat_id,
                    "caption": document.caption[:CAPTION_LIMIT],
                },
                filename=document.path.name,
                content=document.path.read_bytes(),
            )
            self._post("sendDocument", body, content_type, what=f"the file {document.path.name}")
            self.attached += 1

    def _post(self, method: str, body: bytes, content_type: str, *, what: str) -> None:
        """The one place the token is read, and the one place the network is touched.

        Every exception is caught and re-raised carrying only its type name.
        `urllib` puts the full URL into the string form of most of its errors,
        and the URL is where the token is — an unhandled HTTPError would print
        the bot token into a log file or a launchd stderr that nobody reads
        until the day somebody does.
        """
        import urllib.request

        request = urllib.request.Request(
            f"https://api.telegram.org/bot{self._token}/{method}",
            data=body,
            headers={"Content-Type": content_type},
        )
        try:
            with urllib.request.urlopen(request, timeout=60) as response:
                if response.status >= 400:
                    raise DeliveryError(f"telegram refused {what}: HTTP {response.status}")
        except DeliveryError:
            raise
        except Exception as error:  # noqa: BLE001 - the token must not reach the traceback
            raise DeliveryError(
                f"telegram delivery failed on {what}: {type(error).__name__}"
            ) from None


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


def _multipart(*, fields: Mapping[str, str], filename: str, content: bytes) -> tuple[bytes, str]:
    """Build a `multipart/form-data` body for one file upload.

    Written out rather than pulled in. `requests` would be three lines here and
    a dependency that exists for one POST is a dependency the test suite has to
    install to run tests that never make the call.

    The boundary is derived from the payload rather than randomised, so the
    same document produces the same request twice and a test can assert on the
    bytes. It is checked against the content: a boundary that appears inside
    the file would end the part early and truncate the upload silently.
    """
    boundary = "----jobdesk" + sha256(content).hexdigest()[:32]
    while boundary.encode("utf-8") in content:  # pragma: no cover - a hash collision
        boundary += "0"

    parts: list[bytes] = []
    for name, value in fields.items():
        parts.append(
            f"--{boundary}\r\nContent-Disposition: form-data; name=\"{name}\"\r\n\r\n"
            f"{value}\r\n".encode()
        )
    parts.append(
        f"--{boundary}\r\nContent-Disposition: form-data; name=\"document\"; "
        f"filename=\"{filename}\"\r\n"
        "Content-Type: application/octet-stream\r\n\r\n".encode()
    )
    parts.append(content)
    parts.append(f"\r\n--{boundary}--\r\n".encode())
    return b"".join(parts), f"multipart/form-data; boundary={boundary}"
