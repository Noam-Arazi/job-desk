"""The offline demo pipeline.

Session 3 has no scrapers and no analyst yet, so the demo runs the skeleton
end to end on synthetic postings: ingest, normalize through the model layer,
resolve duplicates, and report. It exists to prove the parts fit — orchestrator,
store, policy, gateway, trace — and to give the reproduction promise something
to run in a clean clone with no API key.
"""

from __future__ import annotations

import json
from typing import Any

from . import prompts
from .config import SAMPLES_DIR
from .context import RunContext
from .llm.base import LLMRequest
from .orchestrator import Plan
from .registry import registry
from .store import Posting

NORMALIZE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "title": {"type": "string"},
        "company": {"type": "string"},
        "location": {"type": "string"},
        "work_arrangement": {"type": "string"},
        "years_required": {"type": "integer"},
        "degree_required": {"type": "array", "items": {"type": "string"}},
        "open_degree_clause": {"type": "boolean"},
        "language": {"type": "string"},
    },
    "required": [
        "title",
        "company",
        "location",
        "work_arrangement",
        "years_required",
        "degree_required",
        "open_degree_clause",
        "language",
    ],
    "additionalProperties": False,
}


def load_samples() -> list[dict[str, Any]]:
    path = SAMPLES_DIR / "postings.json"
    return json.loads(path.read_text(encoding="utf-8"))


def normalize_request(posting: dict[str, Any]) -> LLMRequest:
    """Built in one place so the recorder and the run always agree on the key."""
    prompt = prompts.load("normalizer", "normalize_posting", 1)
    return LLMRequest(
        stage="normalize_posting",
        system="You normalize job postings into a fixed structure. You never invent a value.",
        user=prompt.render(
            site=posting["site"],
            title=posting["title"],
            company=posting["company"],
            location=posting.get("location", ""),
            body=posting.get("body", ""),
        ),
        schema=NORMALIZE_SCHEMA,
        max_tokens=1024,
        prompt_id=prompt.id,
        prompt_sha256=prompt.sha256,
    )


# --------------------------------------------------------------------------
# agents
# --------------------------------------------------------------------------


def agent_ingest(ctx: RunContext, inputs: dict[str, Any], upstream: dict[str, Any]) -> dict:
    """Deterministic. Spends no tokens — this is the cut before the model layer."""
    new = 0
    for raw in load_samples():
        posting = Posting(
            site=raw["site"],
            external_id=raw["external_id"],
            title=raw["title"],
            company=raw["company"],
            location=raw.get("location", ""),
            url=raw.get("url", ""),
            body=raw.get("body", ""),
            posted_at=raw.get("posted_at", ""),
        )
        if ctx.store.upsert_posting(posting, now=ctx.now, run_id=ctx.run_id):
            new += 1
    counts = ctx.store.counts()
    return {"ingested": counts["postings"], "distinct_roles": counts["fingerprints"], "new": new}


def agent_normalize(ctx: RunContext, inputs: dict[str, Any], upstream: dict[str, Any]) -> dict:
    limit = int(inputs.get("limit", 10))
    listing = registry.dispatch("list_new_postings", {"limit": limit}, ctx)
    if not listing.ok:
        raise RuntimeError(listing.error or "could not list postings")

    normalized: list[dict[str, Any]] = []
    for posting in listing.content:
        response = ctx.gateway.complete(normalize_request(posting), ctx=ctx)
        record = dict(response.parsed)
        record["fingerprint"] = posting["fingerprint"]
        normalized.append(record)

        registry.dispatch(
            "record_decision",
            {
                "fingerprint": posting["fingerprint"],
                "stage": "normalize_posting",
                "verdict": "pass",
                "reason": "normalized",
            },
            ctx,
        )
    return {"normalized": normalized, "count": len(normalized)}


def agent_resolve(ctx: RunContext, inputs: dict[str, Any], upstream: dict[str, Any]) -> dict:
    """Deterministic dedup. The model only breaks ties, and there are none here."""
    collapsed = []
    for record in upstream.get("normalize", {}).get("normalized", []):
        listings = ctx.store.duplicates_of(record["fingerprint"])
        if len(listings) > 1:
            collapsed.append(
                {
                    "fingerprint": record["fingerprint"],
                    "title": record["title"],
                    "sites": sorted({row["site"] for row in listings}),
                }
            )
    return {"collapsed": collapsed, "count": len(collapsed)}


def agent_report(ctx: RunContext, inputs: dict[str, Any], upstream: dict[str, Any]) -> dict:
    normalize = upstream.get("normalize") or {}
    resolve = upstream.get("resolve") or {}
    return {
        "roles": normalize.get("count", 0),
        "collapsed_duplicates": resolve.get("count", 0),
        "cost_usd": round(ctx.tracer.total.cost_usd, 6),
        "tokens": ctx.tracer.total.input_tokens + ctx.tracer.total.output_tokens,
    }


AGENTS = {
    "ingest": agent_ingest,
    "normalize": agent_normalize,
    "resolve": agent_resolve,
    "report": agent_report,
}


def demo_plan() -> Plan:
    """Hand-written for the demo. Session 7 has the orchestrator emit this."""
    return Plan(
        goal="Ingest the sample postings, normalize them, collapse duplicates and report.",
        steps=[
            {"id": "ingest", "agent": "ingest"},
            {
                "id": "normalize",
                "agent": "normalize",
                "inputs": {"limit": 10},
                "depends_on": ["ingest"],
            },
            {"id": "resolve", "agent": "resolve", "depends_on": ["normalize"]},
            {"id": "report", "agent": "report", "depends_on": ["normalize", "resolve"]},
        ],
    )
