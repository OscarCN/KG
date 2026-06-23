"""
Simulate the production entity stream: document -> extraction -> linking.

This is an integration harness, not the unit runner for either subsystem.
It reads incoming-document fixtures from ``data/<DATA_SUBDIR>/`` and processes
each document independently, extracting entities/events first and immediately
streaming the extracted records into the event linker.

Run:
    ipython src/entities/run_entities.py
or, inside an existing IPython session:
    %run src/entities/run_entities.py

After the script finishes, these names are bound for inspection:

    source_records  - raw fixture documents loaded from INPUT_FILES / DATA_SUBDIR
    articles        - normalized article payloads sent to EntityExtractor
    stream_results  - one summary dict per source document
    all_entities    - flat list of extracted records
    linker          - EntityLinker instance
    link_results    - LinkResult values produced while streaming
    linked          - dict {"events": [...]} (themes/entities skipped by linker)
"""

from __future__ import annotations

import json
import logging
import os
import sys
from collections import Counter
from datetime import date, datetime
from pathlib import Path
from typing import Any, Dict, List

from dotenv import load_dotenv

# Ensure project root is on sys.path.
_PROJECT_ROOT = Path("/Users/oscarcuellar/ocn/media/kg/kg/")
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

# Load OPENROUTER_API_KEY, NLP_URL, GEOCODING_URL, etc. from local env files.
load_dotenv(_PROJECT_ROOT / ".env.local")

logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")
logging.getLogger("src.entities.extraction").setLevel(logging.INFO)
logging.getLogger("src.entities.linking").setLevel(logging.INFO)

from src.entities.document import record_to_article as _record_to_article
from src.entities.extraction.extract import EntityExtractor, Ontology
from src.entities.linking.link import EntityLinker, LinkResult


# -- Configuration ------------------------------------------------------------

# Which subdirectory under ``data/`` to read. This should contain incoming
# document fixtures, usually produced by ``src/PoC/get_entities_data.py`` or
# ``src/PoC/get_data.py``.
DATA_SUBDIR: str = os.environ.get("ENTITIES_DATA_SUBDIR", "ayuntamiento_qro")

# Set to explicit Path values to process specific files, or leave as None to
# stream every ``*.json`` under ``data/<DATA_SUBDIR>/``.
INPUT_FILES: list[Path] | None = None

# Max source documents to process across all files. Set to None for all.
LIMIT: int | None = (
    int(os.environ["ENTITIES_LIMIT"]) if os.environ.get("ENTITIES_LIMIT") else 100
)

# Set to False to skip geocoding. Events with no resolvable state are grouped
# in the empty-location candidate bucket.
GEOCODE: bool = os.environ.get("ENTITIES_GEOCODE", "true").lower() not in {
    "0",
    "false",
    "no",
}

# Persist debug artifacts from the simulated stream.
OUTPUT_STEM: str = os.environ.get("ENTITIES_OUTPUT_STEM", DATA_SUBDIR)
EXTRACTED_OUTPUT: Path = (
    _PROJECT_ROOT / "data" / "extracted_raw" / f"{OUTPUT_STEM}.json"
)
LINKED_OUTPUT: Path = _PROJECT_ROOT / "data" / "linked" / f"{OUTPUT_STEM}.json"


def _json_default(value: Any) -> Any:
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, set):
        return sorted(value)
    raise TypeError(f"Object of type {type(value).__name__} is not JSON serializable")


def _load_source_records(files: list[Path]) -> list[dict]:
    records: list[dict] = []
    for filepath in files:
        with open(filepath, encoding="utf-8") as f:
            payload = json.load(f)
        if not isinstance(payload, list):
            payload = [payload]
        records.extend(payload)
    return records


# -- 1. Load incoming-document fixture ---------------------------------------

data_dir = _PROJECT_ROOT / "data" / DATA_SUBDIR
files = INPUT_FILES if INPUT_FILES is not None else sorted(data_dir.glob("*.json"))

print(f"Data dir: {data_dir}")
print(f"Files found: {len(files)}")
for filepath in files:
    print(f"  {filepath.name}")

source_records = _load_source_records(files)
articles = [_record_to_article(record) for record in source_records]
articles = sorted(
    articles,
    key=lambda article: str(article.get("publication_date") or ""),
)
if LIMIT is not None:
    articles = articles[:LIMIT]

print(f"Incoming documents: {len(articles)}")


# -- 2. Stream documents through extraction and linking -----------------------

print()
print("Setting up extractor and linker ...")
ontology = Ontology()
extractor = EntityExtractor(ontology=ontology)
linker = EntityLinker(geocode=GEOCODE)

stream_results: list[dict[str, Any]] = []
all_entities: list[dict[str, Any]] = []
link_results: list[LinkResult] = []

print()
print("Streaming documents ...")
for i, article in enumerate(articles, start=1):
    source_id = article.get("id") or article.get("url") or ""
    text_preview = (article.get("title") or article.get("text") or "")[:80]
    text_preview = text_preview.replace("\n", " ")

    matched_classes = extractor.match(article)
    print(
        f"[{i}/{len(articles)}] {source_id} "
        f"matched_classes={len(matched_classes)} {text_preview!r}"
    )

    if not matched_classes:
        stream_results.append({
            "source_id": source_id,
            "matched_classes": [],
            "extracted_count": 0,
            "link_statuses": [],
        })
        continue

    try:
        extracted = extractor.extract(
            article,
            validate=True,
            raise_validation_error=False,
        )
    except Exception as exc:
        print(f"  extraction ERROR: {exc}")
        stream_results.append({
            "source_id": source_id,
            "matched_classes": sorted(matched_classes),
            "extracted_count": 0,
            "link_statuses": [],
            "error": str(exc),
        })
        continue

    all_entities.extend(extracted)
    statuses: list[str] = []
    for raw in extracted:
        result = linker.link_one(raw)
        link_results.append(result)
        statuses.append(result.status)

    print(
        f"  extracted={len(extracted)} "
        f"supertypes={dict(Counter(e.get('_supertype', '?') for e in extracted))} "
        f"link_statuses={dict(Counter(statuses))}"
    )
    stream_results.append({
        "source_id": source_id,
        "matched_classes": sorted(matched_classes),
        "extracted_count": len(extracted),
        "link_statuses": statuses,
    })


# -- 3. Persist and print final summary --------------------------------------

linked = {"events": list(linker.events.values())}
status_counts = Counter(result.status for result in link_results)

print()
print(f"Extracted records: {len(all_entities)}")
print(f"  by supertype: {dict(Counter(e.get('_supertype', '?') for e in all_entities))}")
print(f"Linked events: {len(linked['events'])}")
print(f"  link results: {dict(status_counts)}")
if linker.dropped:
    print(f"  dropped: {dict(linker.dropped)}")
print(
    "  events merged from multiple sources: "
    f"{sum(1 for event in linked['events'] if len(event.get('source_ids') or []) > 1)}"
)

EXTRACTED_OUTPUT.parent.mkdir(parents=True, exist_ok=True)
with open(EXTRACTED_OUTPUT, "w", encoding="utf-8") as f:
    json.dump(all_entities, f, ensure_ascii=False, indent=2, default=_json_default)

LINKED_OUTPUT.parent.mkdir(parents=True, exist_ok=True)
with open(LINKED_OUTPUT, "w", encoding="utf-8") as f:
    json.dump(linked, f, ensure_ascii=False, indent=2, default=_json_default)

print(f"Wrote {EXTRACTED_OUTPUT}")
print(f"Wrote {LINKED_OUTPUT}")
