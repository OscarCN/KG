"""Pre-link script for the tags pipeline.

Reads:
  --news        raw news JSON (output of `PoC/get_data.py`); list of docs,
                each with `url`, `text`/`title`, `comments`.
  --extracted   pre-extracted records JSON (output of `extraction/extract.py`);
                list of records, each with `_source_id` (= news.url),
                `_supertype`, and the schema fields the linker reads.

Outputs (under --out-dir):
  <stem>.json             news docs enriched with `event_ids: list[str]`.
  <stem>__events.json     flat dict keyed by event_id, value =
                          {id, description, event_type, name}.

This isolates the tags pipeline from extraction/linking. The output fixture
is what `tags.retrieval.ArticleBundleRetriever` consumes (see design §7.2).

Usage:
    python scripts/build_linked_fixture.py \\
        --news      data/ayuntamiento_qro/ayuntamiento_qro_20260506_175946.json \\
        --extracted data/extracted_raw/ayuntamiento_tst.json \\
        --out-dir   data/linked/
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from collections import defaultdict
from datetime import date, datetime
from pathlib import Path
from typing import Any

_PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from dotenv import load_dotenv

load_dotenv(_PROJECT_ROOT / ".env.local")

logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")
logging.getLogger("src.entities.linking").setLevel(logging.INFO)

from src.entities.linking.link import EntityLinker  # noqa: E402


def _json_default(value: Any) -> Any:
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, set):
        return sorted(value)
    return str(value)


def _event_context_payload(event: dict[str, Any]) -> dict[str, Any]:
    """Compact `LinkedEventContext`-shaped record for the sibling event file."""
    return {
        "id": event.get("event_id") or event.get("id"),
        "description": event.get("description") or "",
        "event_type": event.get("event_type"),
        "name": event.get("name"),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--news", required=True, type=Path,
                        help="raw news JSON (with `url` + `comments`)")
    parser.add_argument("--extracted", required=True, type=Path,
                        help="pre-extracted records JSON")
    parser.add_argument("--out-dir", required=True, type=Path,
                        help="output directory")
    parser.add_argument("--stem", default=None,
                        help="output filename stem (default = news file stem)")
    parser.add_argument("--no-geocode", action="store_true",
                        help="disable geocoding in the linker")
    args = parser.parse_args()

    news_path: Path = args.news
    extracted_path: Path = args.extracted
    out_dir: Path = args.out_dir
    stem = args.stem or news_path.stem

    print(f"[build_linked_fixture] news       = {news_path}")
    print(f"[build_linked_fixture] extracted  = {extracted_path}")
    print(f"[build_linked_fixture] out_dir    = {out_dir}")
    print(f"[build_linked_fixture] stem       = {stem}")

    # ── 1. Load inputs ─────────────────────────────────────────────────
    with open(news_path, encoding="utf-8") as f:
        news_docs: list[dict[str, Any]] = json.load(f)
    with open(extracted_path, encoding="utf-8") as f:
        extracted_records: list[dict[str, Any]] = json.load(f)

    print(f"[build_linked_fixture] news docs={len(news_docs)} "
          f"extracted records={len(extracted_records)}")

    # ── 2. Link extracted records ──────────────────────────────────────
    linker = EntityLinker(geocode=not args.no_geocode)
    event_ids_by_source: dict[str, list[str]] = defaultdict(list)
    n_created = n_merged = n_skipped = n_dropped = 0
    for rec in extracted_records:
        result = linker.link_one(rec)
        if result.status == "created":
            n_created += 1
        elif result.status == "merged":
            n_merged += 1
        elif result.status == "skipped":
            n_skipped += 1
        else:
            n_dropped += 1
        if result.status in ("created", "merged") and result.event_id:
            sid = rec.get("_source_id") or ""
            if sid and result.event_id not in event_ids_by_source[sid]:
                event_ids_by_source[sid].append(result.event_id)

    print(f"[build_linked_fixture] linked: created={n_created} merged={n_merged} "
          f"skipped={n_skipped} dropped={n_dropped}")

    # ── 3. Enrich news docs with event_ids ─────────────────────────────
    out_dir.mkdir(parents=True, exist_ok=True)
    enriched: list[dict[str, Any]] = []
    n_with_events = 0
    for doc in news_docs:
        url = doc.get("url") or ""
        ev_ids = list(event_ids_by_source.get(url, []))
        out = dict(doc)
        out["event_ids"] = ev_ids
        if ev_ids:
            n_with_events += 1
        enriched.append(out)

    docs_path = out_dir / f"{stem}.json"
    with open(docs_path, "w", encoding="utf-8") as f:
        json.dump(enriched, f, ensure_ascii=False, indent=2, default=_json_default)
    print(f"[build_linked_fixture] wrote {docs_path}  "
          f"({len(enriched)} docs, {n_with_events} with event_ids)")

    # ── 4. Write sibling event store ───────────────────────────────────
    events_payload: dict[str, dict[str, Any]] = {}
    for event_id, event in linker.events.items():
        events_payload[event_id] = _event_context_payload(event)

    events_path = out_dir / f"{stem}__events.json"
    with open(events_path, "w", encoding="utf-8") as f:
        json.dump(events_payload, f, ensure_ascii=False, indent=2, default=_json_default)
    print(f"[build_linked_fixture] wrote {events_path}  ({len(events_payload)} events)")

    return 0


if __name__ == "__main__":
    sys.exit(main())
