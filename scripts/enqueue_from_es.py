"""Fetch documents from Elasticsearch over a date window and publish them to the
kg RabbitMQ doc queue — a testing producer for the streaming listener.

Producer-side filter (a geo pre-scope only; keyword filtering stays in the
listener via Ontology.match). The geo rule is the shared `src/geo_scope.py`
one, configured via `FILTER_GEO` (comma-separated geoid prefixes; defaults to
the demo scope `DEMO_FILTER_GEO`): a document is kept iff any
`locations_mentioned` entry matches a prefix — municipio-or-finer prefixes on
`geoid` alone, state-level prefixes additionally requiring
`precision_level >= 3` (city or finer) on that entry.

Then drop documents tagged category "Deportes".

ES is coarse-filtered by `cvegeo` per covering level_2 (derived from the scope
prefixes) — fetched as SEPARATE queries and merged (one cvegeo value per
request, since the FilterRequest AND-combines multiple cvegeo wildcards). The
precise per-entry rule above is applied in Python.

Env (from kg/.env.local): RABBIT_HOST/PORT/USER/PASSWORD/VIRTUALHOST/QUEUE,
ELASTIC_* (used by elastic_client).

Usage:
    python scripts/enqueue_from_es.py --start 2026-05-01 --end 2026-05-02
    python scripts/enqueue_from_es.py --start 2026-05-01 --end 2026-05-02 --dry-run
    python scripts/enqueue_from_es.py --start ... --end ... --limit 200
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

_PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))
load_dotenv(_PROJECT_ROOT / ".env.local")

import pika  # noqa: E402

# get_data adds the sibling elastic_client to sys.path and exposes the helpers.
from src.PoC.get_data import NEWS_FIELDS, fetch_docs  # noqa: E402
from src.geo_scope import DEMO_FILTER_GEO, GeoScope  # noqa: E402

# FILTER_GEO env overrides; the demo scope is the default (the coarse ES fetch
# needs a scope, so unlike the listener this producer never runs unscoped).
GEO_SCOPE = GeoScope.from_env(default=DEMO_FILTER_GEO)
SKIP_CATEGORY = "deportes"


def _flatten_categories(custom_categories: Any) -> set[str]:
    out: set[str] = set()
    if isinstance(custom_categories, dict):
        for vals in custom_categories.values():
            if isinstance(vals, list):
                out.update(str(v).strip().lower() for v in vals)
            elif vals:
                out.add(str(vals).strip().lower())
    elif isinstance(custom_categories, list):
        out.update(str(v).strip().lower() for v in custom_categories)
    return out


def _keep(doc: dict) -> bool:
    if not GEO_SCOPE.matches_doc(doc):
        return False
    return SKIP_CATEGORY not in _flatten_categories(doc.get("custom_categories"))


def _content_blob(doc: dict) -> str:
    """Concatenated free-text fields for content-level exclusion matching."""
    return " ".join(
        str(doc.get(k) or "") for k in ("title", "summary", "text", "url")
    ).lower()


def _publish(docs: list[dict], queue: str) -> None:
    credentials = pika.PlainCredentials(os.environ["RABBIT_USER"], os.environ["RABBIT_PASSWORD"])
    connection = pika.BlockingConnection(
        pika.ConnectionParameters(
            host=os.environ["RABBIT_HOST"],
            port=int(os.environ.get("RABBIT_PORT", 5672)),
            virtual_host=os.environ.get("RABBIT_VIRTUALHOST", "/"),
            credentials=credentials,
        )
    )
    channel = connection.channel()
    channel.queue_declare(queue=queue, durable=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    for i, doc in enumerate(docs):
        doc["trace_id"] = f"es-{stamp}-{i}"
        channel.basic_publish(
            exchange="",
            routing_key=queue,
            body=json.dumps(doc, ensure_ascii=False, default=str).encode("utf-8"),
            properties=pika.BasicProperties(delivery_mode=2),
        )
    connection.close()


def _fetch_window(start: str, end: str) -> dict[str, dict]:
    """Coarse-fetch each covering level_2 separately (cvegeo AND-combines, so one
    value per request), deduped by _id."""
    by_id: dict[str, dict] = {}
    for l2 in GEO_SCOPE.covering_level2s():
        request = {
            "doctype": "news",
            "period": [start, end],
            "cvegeo": [l2],
            "location_type": "mentioned",
            "sort": "date_created",
            "page_size": 5000,
        }
        docs = fetch_docs(request, fields=NEWS_FIELDS)
        for d in docs:
            by_id[str(d.get("_id") or id(d))] = d
        print(f"  cvegeo={l2}: fetched {len(docs)}")
    return by_id


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--start", required=True, help="window start (ISO, e.g. 2026-05-01)")
    parser.add_argument("--end", required=True, help="window end (ISO)")
    parser.add_argument("--limit", type=int, default=None, help="cap on docs published")
    parser.add_argument("--exclude-regex", default=None,
                        help="drop docs whose title/summary/text/url matches this regex (case-insensitive)")
    parser.add_argument("--dry-run", action="store_true", help="filter only; print counts + a sample, do not publish")
    args = parser.parse_args()

    print(f"geo scope: {GEO_SCOPE} (coarse level_2s {GEO_SCOPE.covering_level2s()}); "
          f"drop Deportes")
    by_id = _fetch_window(args.start, args.end)
    kept = [d for d in by_id.values() if _keep(d)]
    if args.exclude_regex:
        pattern = re.compile(args.exclude_regex, re.IGNORECASE)
        before = len(kept)
        kept = [d for d in kept if not pattern.search(_content_blob(d))]
        print(f"exclude-regex {args.exclude_regex!r}: dropped {before - len(kept)} docs")
    if args.limit:
        kept = kept[:args.limit]
    print(f"fetched(unique)={len(by_id)} kept={len(kept)}")

    if args.dry_run:
        for d in kept[:5]:
            print(f"  sample: {d.get('_id')} {(d.get('title') or '')[:70]!r}")
        print("dry-run: nothing published")
        return

    _publish(kept, os.environ["RABBIT_QUEUE"])
    print(f"published {len(kept)} docs -> {os.environ['RABBIT_QUEUE']} "
          f"(vhost {os.environ.get('RABBIT_VIRTUALHOST', '/')})")


if __name__ == "__main__":
    main()
