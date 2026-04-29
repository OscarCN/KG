"""
Fetch documents from Elasticsearch using the `elastic_client` package and
save them to JSON for downstream extraction.

Usage:
    python src/PoC/get_data.py
    GET_DATA_QUERY=legislative_gto python src/PoC/get_data.py

By default this runs the Ayuntamiento de Querétaro query (entity_id 75)
and writes the hits to `data/ayuntamiento_qro/`. Set
`GET_DATA_QUERY=legislative_gto` to run the legislative-initiatives
Guanajuato query instead. The `fetch_docs` and `save_docs` helpers can
also be imported from other scripts.

The `elastic_client` package lives outside this repo at
`/Users/oscarcuellar/ocn/media/elastic_client`; install it editable with
`pip install -e /Users/oscarcuellar/ocn/media/elastic_client` or the
fallback `sys.path` insertion below will pick it up.
"""

from __future__ import annotations

import json
import logging
import os
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable, Optional

from dotenv import load_dotenv

load_dotenv('../.env.local')

# ── Logging ───────────────────────────────────────────────────────────────────

# `elastic_client` attaches a NullHandler to its package logger, so nothing
# is emitted by default. Enable DEBUG-level output for it (and for this
# module) to trace query building, index resolution, and ES calls.
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-7s %(name)s — %(message)s",
)
logging.getLogger("elastic_client").setLevel(logging.DEBUG)
logger = logging.getLogger(__name__)
logger.setLevel(logging.DEBUG)

# ── Path bootstrapping ────────────────────────────────────────────────────────

_PROJECT_ROOT = Path("/Users/oscarcuellar/ocn/media/kg/kg/")
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

# Allow running without `pip install -e` of the sibling elastic_client package
_ELASTIC_CLIENT_DIR = Path("/Users/oscarcuellar/ocn/media/elastic_client")
if _ELASTIC_CLIENT_DIR.exists() and str(_ELASTIC_CLIENT_DIR) not in sys.path:
    sys.path.insert(0, str(_ELASTIC_CLIENT_DIR))

from elastic_client import SearchClient  # noqa: E402


# ── Core helpers ──────────────────────────────────────────────────────────────

def fetch_docs(
    request: dict,
    fields: Optional[list[str]] = None,
    limit: Optional[int] = None,
) -> list[dict]:
    """Build and execute an Elasticsearch search from a FilterRequest-shaped dict.

    Args:
        request: dict matching `elastic_client.FilterRequest` fields
            (`doctype`, `period`, `keywords`, `phrases`, `cvegeo`, ...).
        fields: optional list of source fields to return. When set, the search
            is restricted to these fields via `Search.source(fields)`.
        limit: optional cap on the number of hits returned. When set, overrides
            the request's `page_size` via `Search[0:limit]`.

    Returns:
        List of hit dicts — each is `hit.to_dict()` with `_id` added.
    """
    client = SearchClient()
    logger.debug("fetch_docs request: %s", json.dumps(request, ensure_ascii=False, default=str))
    search, filters_echo = client.build_search(request)
    logger.debug("filters echo: %s", json.dumps(filters_echo, ensure_ascii=False, default=str))

    if fields is not None:
        search = search.source(fields)

    # `QueryBuilder` echoes page_size/page_number but does not apply them to
    # the Search. Without an explicit size, elasticsearch_dsl defaults to 10.
    page_size = filters_echo.get("page_size") or 0
    page_number = filters_echo.get("page_number") or 0
    if limit is not None:
        search = search.extra(from_=0, size=limit)
    elif page_size:
        search = search.extra(from_=page_number * page_size, size=page_size)

    logger.debug("final search body: %s", json.dumps(search.to_dict(), ensure_ascii=False, default=str))

    response = search.execute()
    logger.info(
        "ES returned %d hits (total=%s, took=%sms)",
        len(response.hits),
        getattr(response.hits.total, "value", response.hits.total),
        response.took,
    )

    docs: list[dict] = []
    for hit in response:
        doc = hit.to_dict()
        doc["_id"] = hit.meta.id
        docs.append(doc)
    return docs


def save_docs(
    docs: Iterable[dict],
    dest_dir: Path,
    filename_stem: Optional[str] = None,
) -> Path:
    """Write `docs` as a JSON array to `dest_dir/<filename_stem>_<timestamp>.json`.

    Creates `dest_dir` if missing. Returns the written path.
    """
    dest_dir = Path(dest_dir)
    dest_dir.mkdir(parents=True, exist_ok=True)

    stem = filename_stem or "docs"
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    out_path = dest_dir / f"{stem}_{timestamp}.json"

    docs_list = list(docs)
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(docs_list, f, ensure_ascii=False, indent=2, default=str)

    print(f"Wrote {len(docs_list)} docs to {out_path}")
    return out_path


# ── Sample queries ────────────────────────────────────────────────────────────

# Keywords use the Spanish-analyzed `.esp` fields, so stemming collapses
# inflections (propone/proponen/propuso/proponer → same stem). Listing base
# forms is enough; no need to enumerate conjugations.
LEGISLATIVE_INITIATIVE_GTO_REQUEST: dict = {
    "doctype": "news",
    "period": "w",
    "keywords": {
        "AND": [
            "guanajuato",
            "congreso",
            {
                "OR": [
                    "iniciativa",
                    "propuesta",
                    "proponer",
                    "propuso",
                    "plantear",
                    "impulsar",
                    "promover",
                    "presentar",
                    "exhortar",
                    "exhorto",
                    "dictamen",
                    "dictaminar",
                    "aprobar",
                    "decreto",
                    "decretar",
                    "reforma",
                ],
            },
        ],
    },
    "sort": "date_created",
    "page_size": 1000,
}

# Subset of news fields the extraction pipeline needs. `_id` is always
# attached by `fetch_docs`.
NEWS_FIELDS = [
    "date_created",
    "author_name",
    "source",
    "title",
    "url",
    "text",
    "summary",
    "fb_likes",
    "custom_categories",
    "locations_mentioned",
]


def fetch_legislative_initiatives_gto(
    limit: Optional[int] = None,
) -> list[dict]:
    """Run the sample legislative-initiatives Guanajuato query."""
    return fetch_docs(
        LEGISLATIVE_INITIATIVE_GTO_REQUEST,
        fields=NEWS_FIELDS,
        limit=limit,
    )


# `entity_id` filters on the nested `entities.entity_id` field. 75 is the
# KB id for the "Ayuntamiento de Querétaro" entity — pulling all news
# tagged with it gives a stream of municipal-government content for
# Querétaro without needing keyword heuristics.
AYUNTAMIENTO_QRO_REQUEST: dict = {
    "doctype": "news",
    "period": "w",
    "entity_id": 75,
    "sort": "date_created",
    "page_size": 1000,
}


def fetch_ayuntamiento_qro(
    limit: Optional[int] = None,
) -> list[dict]:
    """Run the default Ayuntamiento de Querétaro query (entity_id=75)."""
    return fetch_docs(
        AYUNTAMIENTO_QRO_REQUEST,
        fields=NEWS_FIELDS,
        limit=limit,
    )


# ── Script entrypoint ─────────────────────────────────────────────────────────

_QUERIES = {
    "ayuntamiento_qro": (
        fetch_ayuntamiento_qro,
        "ayuntamiento_qro",
        "ayuntamiento_qro",
    ),
    "legislative_gto": (
        fetch_legislative_initiatives_gto,
        "legislative_gto",
        "legislative_initiative_gto",
    ),
}


if __name__ == "__main__":
    query_name = os.environ.get("GET_DATA_QUERY", "ayuntamiento_qro")
    if query_name not in _QUERIES:
        raise SystemExit(
            f"Unknown GET_DATA_QUERY={query_name!r}. "
            f"Choose one of: {', '.join(_QUERIES)}"
        )
    fetch_fn, subdir, filename_stem = _QUERIES[query_name]

    out_dir = _PROJECT_ROOT / "data" / subdir

    limit_env = os.environ.get("GET_DATA_LIMIT")
    limit = int(limit_env) if limit_env else None

    docs = fetch_fn(limit=limit)
    save_docs(docs, out_dir, filename_stem=filename_stem)
