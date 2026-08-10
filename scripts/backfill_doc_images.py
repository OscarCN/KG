"""One-off backfill of ``entities_documents.doc_images`` from ES ``news``.

The streaming pipeline now captures each article's ``media_pictures`` at ingest
(extract ``_images`` → linker ``_sources[].images`` → ``doc_images``); rows
written before that change have ``doc_images IS NULL``. This script fills them:

  1. SELECT DISTINCT doc_id FROM entities_documents
     WHERE doc_index='news' AND (doc_images IS NULL OR doc_images = '[]')
     — ``[]`` rows are re-checked too: the 2026-08 ``record_to_article``
     regression wrote [] for docs that actually had pictures.
  2. ES ``news`` search by ids (doc_id == ES ``_id`` == article url), pulling
     only ``media_pictures``.
  3. UPDATE doc_images = [{url, url_md5}, ...] (capped, same normalization as
     extraction). Docs found with no pictures get ``[]``; docs missing from ES
     stay NULL (re-runnable).

Env (kg/.env.local): KGDB_* for Postgres; ELASTIC_HOST/PORT/AUTH(+_HTTP_CERT)
for ES.

Usage:
    python scripts/backfill_doc_images.py --dry-run
    python scripts/backfill_doc_images.py [--batch 500] [--limit N]
    python scripts/backfill_doc_images.py --since 2026-08-07 --until 2026-08-11

``--since`` / ``--until`` scope the repair to a `doc_date_created` window
(half-open), so a run that fixes one regression's damage doesn't re-check
every historical row against ES.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from dotenv import load_dotenv

_PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))
load_dotenv(_PROJECT_ROOT / ".env.local")

import psycopg2  # noqa: E402
import psycopg2.extras  # noqa: E402
import requests  # noqa: E402

from src.entities.extraction.extract import _coerce_images  # noqa: E402


def _kgdb():
    return psycopg2.connect(
        host=os.environ["KGDB_HOST"],
        port=int(os.environ.get("KGDB_PORT", 5432)),
        user=os.environ["KGDB_USER"],
        password=os.environ["KGDB_PASSWORD"],
        dbname=os.environ["KGDB_NAME"],
    )


def _es_search_ids(ids: list[str]) -> dict[str, list[dict]]:
    """doc_id -> normalized image list, for the ids ES knows. An ids `_search`
    (not mget) because ``news`` is an alias over monthly indices."""
    host = os.environ["ELASTIC_HOST"]
    port = os.environ.get("ELASTIC_PORT", "9200")
    auth = tuple((os.environ.get("ELASTIC_AUTH") or ":").split(":", 1))
    cert = os.environ.get("ELASTIC_HTTP_CERT") or False
    resp = requests.post(
        f"https://{host}:{port}/news/_search",
        json={
            "size": len(ids),
            "_source": ["media_pictures"],
            "query": {"ids": {"values": ids}},
        },
        auth=auth,
        verify=cert,
        timeout=30,
    )
    resp.raise_for_status()
    out: dict[str, list[dict]] = {}
    for hit in resp.json()["hits"]["hits"]:
        out[hit["_id"]] = _coerce_images(hit.get("_source") or {})
    return out


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--batch", type=int, default=500, help="ids per ES request")
    parser.add_argument("--limit", type=int, default=None, help="cap on doc_ids processed")
    parser.add_argument("--dry-run", action="store_true", help="fetch + report, no UPDATEs")
    parser.add_argument("--since", help="only doc_date_created >= this date (YYYY-MM-DD)")
    parser.add_argument("--until", help="only doc_date_created < this date (YYYY-MM-DD)")
    args = parser.parse_args()

    # Date scoping keeps a repair run to the window a regression affected
    # instead of re-checking every historical row against ES.
    where = ["doc_index = 'news'",
             "(doc_images IS NULL OR doc_images = '[]'::jsonb)"]
    params: list = []
    if args.since:
        where.append("doc_date_created >= %s")
        params.append(args.since)
    if args.until:
        where.append("doc_date_created < %s")
        params.append(args.until)

    conn = _kgdb()
    with conn.cursor() as cur:
        # `doc_images = '[]'` rows are included: the 2026-08 record_to_article
        # regression dropped media_pictures from every streamed article, so the
        # listener wrote [] ("no images") for docs that actually had them.
        # Genuine no-image docs just get [] again — idempotent.
        cur.execute(
            "SELECT DISTINCT doc_id FROM entities_documents WHERE "
            + " AND ".join(where)
            + " ORDER BY doc_id",
            params,
        )
        doc_ids = [r[0] for r in cur.fetchall()]
    if args.limit:
        doc_ids = doc_ids[: args.limit]
    print(f"doc_ids to backfill: {len(doc_ids)}")

    found = updated = with_images = 0
    for i in range(0, len(doc_ids), args.batch):
        chunk = doc_ids[i : i + args.batch]
        images_by_id = _es_search_ids(chunk)
        found += len(images_by_id)
        with_images += sum(1 for v in images_by_id.values() if v)
        if args.dry_run:
            continue
        with conn.cursor() as cur:
            for doc_id, images in images_by_id.items():
                cur.execute(
                    "UPDATE entities_documents SET doc_images = %s "
                    "WHERE doc_id = %s AND doc_index = 'news' "
                    "AND (doc_images IS NULL OR doc_images = '[]'::jsonb)",
                    (psycopg2.extras.Json(images), doc_id),
                )
                updated += cur.rowcount
        conn.commit()
        print(f"  {min(i + args.batch, len(doc_ids))}/{len(doc_ids)} "
              f"(found={found} with_images={with_images} rows_updated={updated})")

    missing = len(doc_ids) - found
    print(f"done: found={found} with_images={with_images} rows_updated={updated} "
          f"missing_in_es={missing} (left NULL)")
    conn.close()


if __name__ == "__main__":
    main()
