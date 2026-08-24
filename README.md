# job-desk

Every morning: a ranked shortlist of jobs, each with a tailored CV already written, delivered to Telegram. A human reads it and decides.

**The system never applies.** That is not a setting — `submit_application` is registered in the tool registry and denied at the dispatch point, by a policy the registry owns rather than by one the caller installs. The difference is the guarantee: an adversarial review broke the earlier version three ways without any cleverness at all — a context with no hooks, a context with `hooks=None`, and a hook bus somebody assembled without a policy hook — and in all three the handler ran. A jailbreak does not have to argue a model into calling the tool if it can find the call path where the check was never installed. `tests/test_policy.py` walks all three shapes, and `tests/test_injection.py` runs the payload end to end.

> Status: sessions 1-9 are built — scraping over five boards, four of them on, a content-based duplicate resolver, five deterministic gates, the analyst, the tailoring agent, the submission manager and the evals harness. The attached-browser sites and the README's own measurements table are not. The table is waiting on a hand-labelled gold set, by design: `desk label` shows the posting and nothing the system concluded.

## Run it with nothing

No API key, no network, no accounts.

```bash
uv sync --dev && uv run desk demo
```

The demo ingests four synthetic postings, normalizes them through the model layer against recorded cassettes, collapses the duplicate, and writes a trace. One of the four contains a prompt-injection payload; watch what happens to it.

```bash
uv run pytest -q
```

### The daily path, and what each step refuses to do

```
desk fetch     scrape one board            dry run unless --write
desk resolve   collapse duplicates         dry run unless --write
desk analyze   gates, family, requirements, fit    dry run unless --write
desk tailor    cut a CV from its base      dry run unless --write
desk digest    the ranked shortlist        never applies, ever
desk state     move one posting along      the human's act, recorded
```

Every one of them is a dry run by default. That is not caution for its own sake: a prompt edit or a spec change should be observable before it is recorded, because what one command stores is what the next one reads.

Two more exist for measuring rather than running: `desk evals` scores the system against the hand-labelled gold set and prints the measurements table, and `desk baseline` runs the single-agent comparison the table is measured against — one conversation, one model, no gates.

Reading commands, which change nothing: `desk spec` (what counts as a relevant posting), `desk tools` (the registry and its permission tiers), `desk routes` (the stage routing table), `desk prompts` (prompt versions and hashes), `desk label` (build the gold set by hand), `desk trace <path>`.

## How it is put together

```
spec/search.yaml     the only place a filtering criterion lives
prompts/             versioned prompt files, loaded by id and hash-pinned
src/desk/
  policy.py          three permission tiers
  registry.py        tool schemas + handlers, and the one dispatch point
  hooks.py           policy, tracing, redaction and budget as lifecycle hooks
  orchestrator.py    a typed plan with depends_on
  store/             sqlite: postings, fingerprints, applications, decisions
  llm/               three clients behind one Protocol + the stage routing table
  trace.py           append-only JSONL, tokens and cost per step
```

### The five agentic design patterns, and what proves each one

| Pattern | Where it lives | The test that pins it down |
|---|---|---|
| Tool use | `registry.py` | schema↔handler identity in both directions; tool errors return as `tool_result` and never raise; the daily document write is dispatched through it |
| Reflection | `analyst/reflect.py` | generator/evaluator loop on requirement extraction; the span check runs in Python first, so a fabricated requirement is deleted without a model call |
| Planning | `orchestrator.py` | a typed plan whose dependency graph is validated before anything runs |
| Orchestrator + workers | `pipeline.py`, agents | per-step token accounting against a single-agent baseline |
| Memory | `store/` | content fingerprints stable under whitespace, casing and HTML; cross-run dedup; the applied blocklist |

### Permission tiers

```
read          fetch, search, score, resolve      always allowed
write-local   draft a CV, write a tracking entry  approval token required
external      submit_application                  REGISTERED AND ALWAYS DENIED
```

The external tool is registered deliberately. A tool that simply did not exist would prove nothing — a jailbroken model would fail with "unknown tool", which is a much weaker claim than "the model asked, and the boundary held".

**What the daily run actually dispatches, stated plainly.** The daily path is not a tool loop. No agent in it is handed a tool list: each asks the model one narrow question and gets JSON back, and the stages around that are ordinary Python, so the registry does not sit between the analyst and the store. The exception is the act worth gating. `desk tailor --write` cuts the document by dispatching `write_tailored_cv`, which puts the one write-local act in the daily run under the same policy, tracing and redaction hooks as everything else — and `--write` *is* the approval token, so a dry run is denied at the dispatch point rather than by a branch in the caller that could be forgotten. No argument of that tool names a path; the destination is derived from the contract, and both the guardrail suite and `tests/test_tailor.py` fail if the schema ever grows one.

### Model routing

Two rules. Cut deterministically before spending a token — fetching, HTML-to-text, fingerprinting and the hard gates cost nothing. And send mechanical work to the cheapest model with a tighter prompt rather than to a stronger model with a loose one.

| Stage | Model |
|---|---|
| normalize, family routing, dedup tiebreak, no-fabrication verify, orchestrator plan | Haiku 4.5 |
| requirement extraction, fit score, CV tailoring, proposal and outreach drafts | Sonnet 5 |
| weekly calibration, offline eval judge | Opus 5 |

`desk routes` prints the live table. A stage cannot escalate past its declared ceiling; the resolver raises, and a test walks the whole table.

### Threat model

The input is hostile by construction. A job posting is text written by a stranger, and some of it is addressed to the agent.

- **Prompt injection.** Postings are data, never instructions. What stops an injected "submit this application now" is not a prompt telling the model to be careful — it is that the external tier is denied at dispatch, below the model. `tests/test_injection.py` runs the payload end to end and asserts no external handler was entered.
- **Credential exfiltration.** A redaction hook strips anything key-shaped out of tool results before it can reach a model or the trace. Real data — CVs, application history, the store — is gitignored; the repo carries a fabricated profile.
- **Runaway spend.** Every model call goes through one gateway with a cost ceiling. Crossing it fires `on_budget_exceeded` once and aborts the run cleanly, mid-plan, with partial results reported.
- **Silent breakage.** A site changing its markup fails that module only. The run completes and says which source is missing rather than producing a quietly shorter digest.

### Libraries considered

**Scrapling** is the fetch layer, for adaptive element relocation after a site changes its structure. It stays an implementation detail behind each site module, and it is an optional dependency group so the offline path stays light. One correction earned in use: importing `scrapling.fetchers` pulls curl_cffi and playwright at module load, so the extra that installs "just the HTTP path" has to install them too — a split that looked clean in the dependency table and failed at the first request.

**DeepSeek's agent framework** was rejected — not on quality. It supplies precisely what this repo is meant to demonstrate the author can build: an orchestrator, a tool registry and a policy layer. A repo that assembles those from a dependency shows you can configure a framework, not write one.

### What the fetch layer does not do

What is *not* done anywhere here is the part worth keeping straight, because "scraping politely" and "evading a bot defence" are different acts and only one of them happens in this repo. No login, no cookie, no session, no TLS impersonation, no anti-bot bypass, no CAPTCHA solving, and nothing behind an authentication wall — only the endpoints LinkedIn serves to a logged-out browser, at one request every two seconds. `stealth: false` is not a setting that could be flipped: the fetch layer has no bypass in it to switch on.
