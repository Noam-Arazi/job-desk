"""The subscription path: shell out to `claude -p`.

This is what the daily run uses. Marginal cost is effectively zero, so the cost
figure the trace records here is the list-price equivalent — what the same work
would have cost on the API — rather than money actually spent. The trace labels
it, so the eval table is never accidentally read as a bill.

**The flags below are the whole correctness of this module, and they were
missing.** `claude -p` is not a model endpoint by default — it is an agent. Run
bare, it loads the operator's own `CLAUDE.md`, the project's settings, every
MCP server configured on the machine, and the full built-in tool set, in the
working directory it happens to be started from. The first live run of this
client against real postings did exactly that: every `extract_requirements`
call came back as Hebrew prose about the state of this repository, which the
agent had read off the disk, and not one of them was JSON.

That is the visible half. The other half is the reason this is a security fix
and not a formatting one. A job posting is hostile text by construction — the
threat model in the README says so, and `tests/test_injection.py` runs a payload
through the analyst. Handing that text to a subprocess holding Bash, Write and
a live MCP surface, rooted in the repository, means an injected instruction had
real tools within reach. The registry's policy layer never covered this path:
it governs what *this* process dispatches, and the subprocess was a second
agent nobody had told about the rules.

So the call is stripped down to what it is supposed to be:

    --system-prompt          replaces the agent prompt; nothing survives of it
    --setting-sources ""     no user, project or local settings, no CLAUDE.md
    --strict-mcp-config      no MCP servers, whatever the machine has configured
    --disallowedTools ...    every built-in tool, named
    cwd=a neutral directory  no repository in reach even if a tool got through

Four independent layers, because each one alone is a single edit away from
being wrong, and `tests/test_routing.py` asserts every one of them by name.

**And the schema has to travel with the question.** `LLMRequest.schema` reaches
the API client as the API's own structured-output contract, enforced before a
token comes back. This path has no such channel, and for as long as it sent
only `req.user` the model was being validated against a schema it had never
been shown. It did not fail loudly: it guessed. Live extraction came back
keyed `requirement`/`type` where the schema says `text`/`kind`/`mandatory`,
sometimes fenced in markdown, sometimes carrying an invented `preferences` key
— and the gateway's retry, told only that its answer was wrong, complied by
returning an empty list. Every posting that reached extraction stopped there
with nothing found, which reads in the run summary exactly like a posting that
genuinely stated no requirements.

So the schema is rendered into the message. This is not regexing an answer into
shape — the rule against that stands, and `_validate` is unchanged. It is
giving the model the contract it is about to be judged by.

**The route's effort is sent, because the table is not a description.** Every
stage declares an effort level and this client used to send none of them, so
`desk routes` printed a ceiling the subscription path did not honour: mechanical
extraction ran at whatever the operator's session happened to be set to. It is
visible in the tokens — a `{"requirements": []}` answer that cost fourteen
hundred output tokens is a model thinking hard about a question routed as
cheap — and it is the difference between a nightly pass over four thousand
postings finishing before morning and not finishing at all.

**One transport wrapper is unwrapped, and only one.** Asked for JSON and told
not to fence it, this CLI fences it anyway on some stages and not others —
`reflect_anchors` came back as a complete, correct verdict object inside a
```json block, and `json.loads` rejected the backticks. Stripping that fence is
not coercing an answer into a schema: the bytes inside are returned untouched
and `_validate` judges them exactly as before. `_unfence` is deliberately
narrow — the whole response must be one fenced block and nothing else, so a
model that wrote prose *around* a code block still fails, which is the case the
no-regex rule exists to catch.
"""

from __future__ import annotations

import json
import subprocess
import tempfile
from dataclasses import dataclass, field
from typing import Any

from ..trace import Usage
from .base import LLMRequest, LLMResponse
from .routing import MODELS, Route, cost_usd


class ClaudeCodeError(RuntimeError):
    pass


ENDPOINT_PROMPT = (
    "You are a single-turn extraction endpoint, not an assistant and not an agent. "
    "You answer only from the text given to you in the message. "
    "You never read files, run commands, browse, or use any tool. "
    "When the message asks for JSON you return exactly one JSON value and nothing "
    "else: no prose before or after it, no explanation, no markdown code fences."
)

# Named individually rather than passed as a wildcard: a tool added to the CLI
# after this list was written must show up as a test failure, not as a silently
# widened surface. `tests/test_routing.py` fails if any of these is dropped.
DISALLOWED_TOOLS = (
    "Bash",
    "Read",
    "Write",
    "Edit",
    "NotebookEdit",
    "Glob",
    "Grep",
    "WebFetch",
    "WebSearch",
    "Task",
    "TodoWrite",
    "SlashCommand",
    "KillShell",
    "BashOutput",
)


def _reason(stdout: str) -> str:
    """The CLI's own explanation, when it wrote one as JSON on a failing exit."""
    try:
        payload = json.loads(stdout)
    except (json.JSONDecodeError, TypeError):
        return stdout[:400].strip()
    if not isinstance(payload, dict):
        return stdout[:400].strip()
    for key in ("result", "error", "message"):
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return value[:400]
    return ""


def _unfence(text: str) -> str:
    """Remove a markdown code fence that wraps the entire response.

    All four conditions must hold: the text opens with a fence, closes with
    one, and there is nothing outside them. Anything else is returned untouched
    and left to fail validation, which is the point — a response with commentary
    around a code block is a model that ignored the instruction, and that is
    worth a retry rather than a salvage.
    """
    stripped = text.strip()
    if not stripped.startswith("```") or not stripped.endswith("```"):
        return text
    inner = stripped[3:-3]
    # An opening fence may name a language: ```json. Drop that word, not a line
    # of content — so only when the first line has no other text on it.
    first, newline, rest = inner.partition("\n")
    if newline and first.strip().isalpha():
        inner = rest
    if "```" in inner:  # two separate blocks, not one wrapper
        return text
    return inner.strip()


def _with_schema(user: str, schema: dict[str, Any] | None) -> str:
    """Append the JSON schema to the question, when there is one to meet.

    Verbatim, and last. The model is being validated against this exact object
    by `_validate`, so paraphrasing it here would create a second version of the
    contract that could drift from the one that judges the answer.
    """
    if schema is None:
        return user
    rendered = json.dumps(schema, ensure_ascii=False, sort_keys=True, indent=2)
    return (
        f"{user}\n\n"
        "Your entire answer must be one JSON value matching this schema exactly. "
        "Use these key names and no others. No prose, no markdown fences.\n\n"
        f"{rendered}"
    )


@dataclass
class ClaudeCodeClient:
    binary: str = "claude"
    timeout_seconds: int = 300  # never unbounded — an untimed job hangs forever
    # The directory the subprocess runs in. `tempfile.gettempdir()` rather than
    # the repo root, so that no project file is reachable by a relative path.
    cwd: str = field(default_factory=tempfile.gettempdir)
    name: str = "claude-code"

    def complete(self, req: LLMRequest, route: Route) -> LLMResponse:
        cmd = [
            self.binary,
            "-p",
            _with_schema(req.user, req.schema),
            "--output-format",
            "json",
            "--model",
            route.model,
            # Replace, never append. `--append-system-prompt` leaves the agent
            # prompt in place, and the agent is what has to go.
            "--system-prompt",
            (req.system + "\n\n" + ENDPOINT_PROMPT) if req.system else ENDPOINT_PROMPT,
            "--setting-sources",
            "",
            "--strict-mcp-config",
        ]
        # Haiku does not accept an effort level — the API returns 400 for it,
        # and `routing.py` records that as `supports_effort`. The same rule is
        # honoured here rather than re-derived, so the two paths cannot drift.
        if route.effort and MODELS[route.ceiling].supports_effort:
            cmd += ["--effort", route.effort]
        # Last, and last on purpose: it is the one variadic flag, so anything
        # appended after it would be swallowed as another tool name.
        cmd += ["--disallowedTools", *DISALLOWED_TOOLS]

        try:
            proc = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=self.timeout_seconds,
                check=False,
                # Not the repository, and not wherever the caller happened to
                # be. A tool that somehow ran still finds an empty directory.
                cwd=self.cwd,
            )
        except FileNotFoundError as exc:
            raise ClaudeCodeError(f"{self.binary} not found on PATH") from exc
        except subprocess.TimeoutExpired as exc:
            raise ClaudeCodeError(f"{self.binary} timed out after {self.timeout_seconds}s") from exc

        if proc.returncode != 0:
            # The CLI reports why it failed on stdout, as JSON, and leaves stderr
            # empty. Reading only stderr produced "claude exited 1: " — an error
            # that tells the reader nothing at the moment they most need to know
            # whether the run died on their credentials, their quota or a bug.
            raise ClaudeCodeError(
                f"{self.binary} exited {proc.returncode}: "
                f"{_reason(proc.stdout) or proc.stderr[:400] or 'no output'}"
            )

        try:
            payload = json.loads(proc.stdout)
        except json.JSONDecodeError as exc:
            raise ClaudeCodeError(f"could not parse CLI output: {proc.stdout[:400]}") from exc

        if payload.get("is_error"):
            raise ClaudeCodeError(str(payload.get("result", "unknown CLI error")))

        raw_usage = payload.get("usage") or {}
        usage = Usage(
            input_tokens=int(raw_usage.get("input_tokens", 0)),
            output_tokens=int(raw_usage.get("output_tokens", 0)),
            cache_read_tokens=int(raw_usage.get("cache_read_input_tokens", 0)),
        )
        usage.cost_usd = cost_usd(
            route.model, usage.input_tokens, usage.output_tokens, usage.cache_read_tokens
        )

        return LLMResponse(
            text=_unfence(str(payload.get("result", ""))),
            usage=usage,
            model=route.model,
            stage=req.stage,
            raw={"engine": "claude-code", "cost_is_list_price_equivalent": True},
        )
