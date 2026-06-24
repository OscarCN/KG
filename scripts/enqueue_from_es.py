"""Fetch documents from Elasticsearch over a date window and publish them to the
kg RabbitMQ doc queue — a testing producer for the streaming listener.

Producer-side filter (a geo pre-scope only; keyword filtering stays in the
listener via Ontology.match):

  - keep a document iff it has a `locations_mentioned` entry whose `level_2_id`
    is one of TARGET_LEVEL2 (default 48409, 48422) **and that same entry** has
    `precision_level >= 3` (city or finer);
  - drop documents tagged category "Deportes".

ES is coarse-filtered by `cvegeo` (wildcard over the target level_2 ids); the
precise per-entry (id AND precision) rule is applied in Python, since it isn't
expressible in the FilterRequest.

Env (from kg/.env.local): RABBIT_HOST/PORT/USER/PASSWORD/VIRTUALHOST/QUEUE,
ELASTIC_* (used by elastic_client).

Usage:
    python scripts/enqueue_from_es.py --start 2026-05-01 --end 2026-05-08
    python scripts/enqueue_from_es.py --start 2026-05-01 --end 2026-05-08 --dry-run
    python scripts/enqueue_from_es.py --start ... --end ... --limit 500 --level2 48409,48422
"""

from __future__ import annotations

import argparse
import json
import os
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

TARGET_LEVEL2 = {"48409", "48422"}
MIN_PRECISION = 3
SKIP_CATEGORY = "deportes"


def _norm_id(value: Any) -> str:
    return str(value or "").lstrip("_").strip()


def _precision(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return -1


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


def _keep(doc: dict, targets: set[str]) -> bool:
    geo_ok = any(
        _norm_id(loc.get("level_2_id")) in targets
        and _precision(loc.get("precision_level")) >= MIN_PRECISION
        for loc in (doc.get("locations_mentioned") or [])
    )
    if not geo_ok:
        return False
    return SKIP_CATEGORY not in _flatten_categories(doc.get("custom_categories"))


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


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--start", required=True, help="window start (ISO, e.g. 2026-05-01)")
    parser.add_argument("--end", required=True, help="window end (ISO)")
    parser.add_argument("--level2", default=None, help="comma-separated level_2_id targets (default 48409,48422)")
    parser.add_argument("--limit", type=int, default=None, help="max ES hits to fetch")
    parser.add_argument("--dry-run", action="store_true", help="filter only; print counts + a sample, do not publish")
    args = parser.parse_args()

    targets = {_norm_id(t) for t in args.level2.split(",")} if args.level2 else set(TARGET_LEVEL2)

    request = {
        "doctype": "news",
        "period": [args.start, args.end],
        "cvegeo": sorted(targets),
        "location_type": "mentioned",
        "sort": "date_created",
        "page_size": args.limit or 5000,
    }
    print(f"ES request: {json.dumps(request, ensure_ascii=False)}")
    docs = fetch_docs(request, fields=NEWS_FIELDS, limit=args.limit)
    kept = [d for d in docs if _keep(d, targets)]
    print(
        f"fetched={len(docs)} kept={len(kept)} "
        f"(dropped {len(docs) - len(kept)}: no level_2 {sorted(targets)} @ precision>={MIN_PRECISION}, or Deportes)"
    )

    if args.dry_run:
        for d in kept[:3]:
            print(f"  sample: {d.get('_id')} {(d.get('title') or '')[:70]!r}")
        print("dry-run: nothing published")
        return

    _publish(kept, os.environ["RABBIT_QUEUE"])
    print(f"published {len(kept)} docs -> {os.environ['RABBIT_QUEUE']} "
          f"(vhost {os.environ.get('RABBIT_VIRTUALHOST', '/')})")


if __name__ == "__main__":
    main()
