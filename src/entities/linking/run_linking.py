"""
Run extracted fixture records through the event linker.

Designed to be run step-by-step in IPython:
    ipython src/entities/linking/run_linking.py
or, inside an existing IPython session:
    %run src/entities/linking/run_linking.py

Run shape:

    1. Load extracted records from INPUT.
    2. Group records by `_source_id` and process sources in publication order.
    3. Stream each extracted record through `EntityLinker.link_one(raw)`.
    4. Write canonical linked events to OUTPUT.

After the script finishes, these names are bound for inspection:

    records        - list of raw extracted records loaded from INPUT
    records_by_source
    source_ids_in_order
    linker         - the EntityLinker instance
    link_results   - list of LinkResult values produced by the run
    linked         - dict {"events": [...]}  (themes/entities are skipped)
"""

from __future__ import annotations

import json
import logging
import os
import sys
from collections import Counter, defaultdict
from datetime import date, datetime
from pathlib import Path
from typing import Any, Dict, List

from dotenv import load_dotenv

# Ensure project root is on sys.path.
_PROJECT_ROOT = Path("/Users/oscarcuellar/ocn/media/kg/kg/")
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

# Load OPENROUTER_API_KEY etc. from .env.local at the project root.
load_dotenv(_PROJECT_ROOT / ".env.local")

logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")
logging.getLogger("src.entities.linking").setLevel(logging.INFO)

from src.entities.linking.link import EntityLinker, LinkResult

# -- Configuration ------------------------------------------------------------
# INPUT/OUTPUT stems are overridable via env so the same harness can drive any
# fixture (e.g. LINK_STEM=geo_qro_public_works_event) without editing the file.

_STEM = os.environ.get("LINK_STEM", "ayuntamiento_tst")
INPUT: Path = _PROJECT_ROOT / "data" / "extracted_raw" / f"{os.environ.get('LINK_INPUT_STEM', _STEM)}.json"
OUTPUT: Path = _PROJECT_ROOT / "data" / "linked" / f"{os.environ.get('LINK_OUTPUT_STEM', _STEM)}.json"

# Set to False to skip geocoding. Events with no resolvable state are grouped
# in the empty-location candidate bucket.
GEOCODE: bool = True


def _json_default(value: Any) -> Any:
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, set):
        return sorted(value)
    raise TypeError(f"Object of type {type(value).__name__} is not JSON serializable")


# -- 1. Load extracted fixture records ---------------------------------------

print(f"Reading {INPUT}")
records: List[Dict[str, Any]] = json.loads(INPUT.read_text(encoding="utf-8"))

print(f"  loaded {len(records)} records")
print(f'  by supertype: {dict(Counter(r.get("_supertype", "?") for r in records))}')

records_by_source: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
for record in records:
    records_by_source[record.get("_source_id") or ""].append(record)

source_ids_in_order = sorted(
    records_by_source.keys(),
    key=lambda source_id: min(
        record.get("date_created") or "" for record in records_by_source[source_id]
    ),
)
print(f"  unique source_ids: {len(source_ids_in_order)}")


# -- 2. Run linker ------------------------------------------------------------

print()
print("Setting up linker ...")
CASE_LOG: Path = _PROJECT_ROOT / "data" / ".runlogs" / f"linking_cases_{OUTPUT.stem}.jsonl"
linker = EntityLinker(geocode=GEOCODE, case_log_path=CASE_LOG)
link_results: List[LinkResult] = []

print()
print("Streaming extracted records ...")
for i, source_id in enumerate(source_ids_in_order, start=1):
    article_records = records_by_source[source_id]
    print(f"[{i}/{len(source_ids_in_order)}] {source_id} records={len(article_records)}")

    for raw in article_records:
        result = linker.link_one(raw)
        link_results.append(result)

status_counts = Counter(result.status for result in link_results)


# -- 3. Persist and print final summary --------------------------------------

linked = {"events": list(linker.events.values())}

print()
print(f"Linked: events={len(linked['events'])}")
print(f"  input -> output: {len(records)} -> {len(linked['events'])}")
print(f"  link results: {dict(status_counts)}")
if linker.dropped:
    print(f"  dropped: {dict(linker.dropped)}")

OUTPUT.parent.mkdir(parents=True, exist_ok=True)
with open(OUTPUT, "w", encoding="utf-8") as f:
    json.dump(linked, f, ensure_ascii=False, indent=2, default=_json_default)

print(f"Wrote {OUTPUT}")
print(
    "  events merged from multiple sources: "
    f"{sum(1 for event in linked['events'] if len(event.get('source_ids') or []) > 1)}"
)
