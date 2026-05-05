"""
Run the entity linker on an extracted-records JSON file.

Designed to be run step-by-step in IPython:
    ipython src/entities/linking/run_linking.py

Or interactively in a Jupyter/IPython session using %run:
    %run src/entities/linking/run_linking.py

After the script finishes, the following names are available for inspection:

    records       — list of raw extracted records loaded from INPUT
    linker        — the EntityLinker instance (with `dropped` counters etc.)
    linked        — dict {"events": [...]}  (themes/entities are skipped)

The input file is expected to be a JSON list of records emitted by
`src/entities/extraction/extract.py` (each carrying `_source_id` and
`_supertype`). A robust loader falls back to a record-boundary scan when
the file is malformed.
"""

from __future__ import annotations

import json
import re
import sys
import logging
from collections import Counter
from datetime import date, datetime
from pathlib import Path
from typing import Any, Dict, List

from dotenv import load_dotenv

# Ensure project root is on sys.path
_PROJECT_ROOT = Path("/Users/oscarcuellar/ocn/media/kg/kg/")
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

# Load OPENROUTER_API_KEY etc. from .env.local at the project root.
load_dotenv(_PROJECT_ROOT / ".env.local")

# Verbose debug logging for the linking pipeline (LLM prompts, link decisions,
# per-event summary at the end). Other libraries stay at WARNING.
logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")
logging.getLogger("src.entities.linking").setLevel(logging.DEBUG)

from src.entities.linking.link import EntityLinker

# ── Configuration ─────────────────────────────────────────────────────────────

# Path to the extracted records JSON (output of src/entities/extraction/extract.py)
INPUT: Path = _PROJECT_ROOT / "data" / "extracted_raw" / "ayuntamiento_tst.json"

# Where to write the linked output.
OUTPUT: Path = _PROJECT_ROOT / "data" / "linked" / "ayuntamiento_tst.json"

# Set to False to skip geocoding (events will then be dropped for low precision).
GEOCODE: bool = True

# ── Robust JSON loader ────────────────────────────────────────────────────────

_SUP_END_RE = re.compile(r'"_supertype":\s*"[^"]+"\s*\}')


def _find_record_start(data: str, end_idx: int) -> int | None:
    """Walk back from ``end_idx`` (the closing `}` position) finding the
    matching `{`. Strings are treated as opaque (a `"` toggles in_str
    unless preceded by an odd number of backslashes).
    """
    in_str = False
    depth = 1
    i = end_idx - 1
    while i >= 0:
        c = data[i]
        if c == '"':
            bs = 0
            j = i - 1
            while j >= 0 and data[j] == "\\":
                bs += 1
                j -= 1
            if bs % 2 == 0:
                in_str = not in_str
        elif not in_str:
            if c == "}":
                depth += 1
            elif c == "{":
                depth -= 1
                if depth == 0:
                    return i
        i -= 1
    return None


def _load_records(path: Path) -> List[Dict[str, Any]]:
    """Load records from the JSON file. Falls back to a record-boundary
    scan when the file is malformed (the test fixture currently is)."""
    text = path.read_text(encoding="utf-8")
    try:
        parsed = json.loads(text)
        if isinstance(parsed, list):
            return parsed
        if isinstance(parsed, dict):
            return [parsed]
        raise ValueError(f"Unexpected top-level JSON type {type(parsed).__name__}")
    except json.JSONDecodeError as ex:
        print(f"  (top-level JSON parse failed: {ex}; falling back to record scan)")

    out: List[Dict[str, Any]] = []
    for m in _SUP_END_RE.finditer(text):
        start = _find_record_start(text, m.end() - 1)
        if start is None:
            continue
        try:
            out.append(json.loads(text[start : m.end()]))
        except json.JSONDecodeError:
            continue
    return out


def _json_default(value: Any) -> Any:
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, set):
        return sorted(value)
    raise TypeError(f"Object of type {type(value).__name__} is not JSON serializable")


# ── Load records ──────────────────────────────────────────────────────────────

print(f"Reading {INPUT}")
records = _load_records(INPUT)
print(f"  loaded {len(records)} records")

sup_counts = Counter(r.get("_supertype", "?") for r in records)
print(f"  by supertype: {dict(sup_counts)}")

# ── Link ──────────────────────────────────────────────────────────────────────

linker = EntityLinker(geocode=GEOCODE)
linked = linker.link_all(records)

n_in = len(records)
n_events = len(linked["events"])
print()
print(f"Linked: events={n_events}")
print(f"  input → output: {n_in} → {n_events}")
if linker.dropped:
    print(f"  dropped: {dict(linker.dropped)}")

# ── Write output ──────────────────────────────────────────────────────────────

OUTPUT.parent.mkdir(parents=True, exist_ok=True)
with open(OUTPUT, "w", encoding="utf-8") as f:
    json.dump(linked, f, ensure_ascii=False, indent=2, default=_json_default)
print(f"Wrote {OUTPUT}")

multi = sum(1 for r in linked["events"] if len(r.get("source_ids") or []) > 1)
print(f"  events merged from multiple sources: {multi}")

# ── Inspect results ────────────────────────────────────────────────────────────
#
# After running, the following names are bound:
#
#   records         — raw extracted records loaded from INPUT
#   linker          — EntityLinker instance (linker.dropped, linker.events, ...)
#   linked          — dict with an "events" list (themes/entities are skipped)
#
# Example inspection:
#
#   linked["events"][0]
#   [e for e in linked["events"] if len(e["source_ids"]) > 1]
#   linker.dropped
