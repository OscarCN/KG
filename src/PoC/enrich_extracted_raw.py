"""
Enrich an `extracted_raw/*.json` fixture with the source article's
`date_created` (publication timestamp) by joining each record's
`_source_id` URL against Elasticsearch.

Designed to be run step-by-step in IPython:
    ipython src/PoC/enrich_extracted_raw.py

Or interactively in a Jupyter/IPython session using %run:
    %run src/PoC/enrich_extracted_raw.py

The fixture is parsed with a robust record-boundary fallback (the
ayuntamiento_tst.json file is malformed JSON), URLs are batched against
the `news` index via `terms`, and the file is rewritten as a clean
JSON list with `date_created` set on each record. Records whose URL
yields no ES hit are left without the field.
"""

from __future__ import annotations

import json
import re
import sys
from collections import Counter
from pathlib import Path
from typing import Any, Dict, List

from dotenv import load_dotenv

# Project root + env (ELASTIC_HOST, ELASTIC_PORT, ELASTIC_AUTH, ELASTIC_HTTP_CERT)
_PROJECT_ROOT = Path("/Users/oscarcuellar/ocn/media/kg/kg/")
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))
load_dotenv(_PROJECT_ROOT / ".env.local")

# Sibling elastic_client package
_ELASTIC_CLIENT_DIR = Path("/Users/oscarcuellar/ocn/media/elastic_client")
if _ELASTIC_CLIENT_DIR.exists() and str(_ELASTIC_CLIENT_DIR) not in sys.path:
    sys.path.insert(0, str(_ELASTIC_CLIENT_DIR))

from elastic_client import SearchClient  # noqa: E402

# ── Configuration ─────────────────────────────────────────────────────────────

INPUT: Path = _PROJECT_ROOT / "data" / "extracted_raw" / "ayuntamiento_tst.json"
OUTPUT: Path = INPUT  # overwrite in place
BATCH_SIZE: int = 100  # URLs per ES query
INDEX: str = "news"
DATE_FIELD: str = "date_created"

# ── Robust record loader (mirrors run_linking.py) ────────────────────────────

_SUP_END_RE = re.compile(r'"_supertype":\s*"[^"]+"\s*\}')


def _find_record_start(data: str, end_idx: int) -> int | None:
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
    text = path.read_text(encoding="utf-8")
    try:
        parsed = json.loads(text)
        if isinstance(parsed, list):
            return parsed
        if isinstance(parsed, dict):
            return [parsed]
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


# ── ES lookup ────────────────────────────────────────────────────────────────


def _fetch_dates(urls: List[str]) -> Dict[str, str]:
    """Return {url → date_created} for any URLs found in ES."""
    client = SearchClient()
    out: Dict[str, str] = {}
    for i in range(0, len(urls), BATCH_SIZE):
        batch = urls[i : i + BATCH_SIZE]
        s = (
            client.raw_search(INDEX)
            .filter("terms", url=batch)
            .source(["url", DATE_FIELD])
            .extra(size=len(batch) * 2)
        )
        resp = s.execute()
        for h in resp:
            doc = h.to_dict()
            url = doc.get("url")
            date_val = doc.get(DATE_FIELD)
            if url and date_val and url not in out:
                out[url] = str(date_val)
        print(f"  batch {i // BATCH_SIZE + 1}: queried {len(batch)} → {len(resp.hits)} hits")
    return out


# ── Main ──────────────────────────────────────────────────────────────────────

print(f"Loading {INPUT}")
records = _load_records(INPUT)
print(f"  loaded {len(records)} records")

urls = sorted({r["_source_id"] for r in records if r.get("_source_id")})
print(f"  unique source URLs: {len(urls)}")

print("Querying ES…")
url_to_date = _fetch_dates(urls)
print(f"  resolved {len(url_to_date)}/{len(urls)} URLs")

missing = [u for u in urls if u not in url_to_date]
if missing:
    print(f"  {len(missing)} URLs without a hit (sample):")
    for u in missing[:5]:
        print(f"    {u}")

added = 0
for r in records:
    sid = r.get("_source_id")
    date_val = url_to_date.get(sid) if sid else None
    if date_val:
        r["date_created"] = date_val
        added += 1
print(f"Added date_created on {added} records")

print(f"Writing {OUTPUT}")
with open(OUTPUT, "w", encoding="utf-8") as f:
    json.dump(records, f, ensure_ascii=False, indent=2)

# Sanity stats
sup_counts = Counter(r.get("_supertype", "?") for r in records)
print(f"  by supertype: {dict(sup_counts)}")
print("Done.")
