"""Regenerate the demo cassettes.

The cassettes committed to this repo are canned answers for the four synthetic
sample postings, not recordings of real traffic — they exist so a clean clone
runs offline. From session 5 the eval cassettes are recorded from real runs with
`RecordingClient`; these stay hand-authored because the postings themselves are
fabricated.

    uv run python -m desk.tools.mkcassettes
"""

from __future__ import annotations

import json
from pathlib import Path

from ..config import CASSETTES_DIR
from ..llm.routing import resolve
from ..pipeline import load_samples, normalize_request

# Keyed by external_id: what a correct normalizer returns for each sample.
ANSWERS: dict[str, dict[str, object]] = {
    "AJ-10241": {
        "title": "אנליסט נתונים",
        "company": "Bluewick Systems",
        "location": "חיפה",
        "work_arrangement": "hybrid",
        "years_required": 2,
        "degree_required": [],
        "open_degree_clause": True,
        "language": "he",
    },
    "DR-88120": {
        "title": "אנליסט נתונים",
        "company": "Bluewick Systems",
        "location": "חיפה",
        "work_arrangement": "hybrid",
        "years_required": 2,
        "degree_required": [],
        "open_degree_clause": True,
        "language": "he",
    },
    "GF-3391": {
        "title": "AI Engineer",
        "company": "Cordelia Labs",
        "location": "Tel Aviv",
        "work_arrangement": "onsite",
        "years_required": 5,
        "degree_required": ["computer science", "software engineering"],
        "open_degree_clause": False,
        "language": "en",
    },
    # The hostile instruction embedded in this posting is normalized as ordinary
    # content. Nothing in the answer acts on it.
    "AJ-10598": {
        "title": "מנהל פרויקטים טכנולוגיים",
        "company": "Nortree Group",
        "location": "נתניה",
        "work_arrangement": "onsite",
        "years_required": 1,
        "degree_required": [],
        "open_degree_clause": True,
        "language": "he",
    },
}

USAGE = {"input_tokens": 620, "output_tokens": 95, "cache_read_tokens": 0}


def main(directory: Path | None = None) -> int:
    out = Path(directory or CASSETTES_DIR)
    out.mkdir(parents=True, exist_ok=True)
    route = resolve("normalize_posting")

    written = 0
    for posting in load_samples():
        answer = ANSWERS[posting["external_id"]]
        request = normalize_request(posting)
        key = request.cassette_key(route)
        (out / f"{key}.json").write_text(
            json.dumps(
                {
                    "stage": request.stage,
                    "model": route.model,
                    "effort": route.effort,
                    "external_id": posting["external_id"],
                    "text": json.dumps(answer, ensure_ascii=False, sort_keys=True),
                    "usage": USAGE,
                },
                indent=2,
                ensure_ascii=False,
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )
        written += 1
        print(f"{posting['external_id']} -> {key}.json")
    return written


if __name__ == "__main__":
    main()
