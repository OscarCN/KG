"""
Build large-scale geo-event linking fixtures: one fixture per geo-heavy
**supertype**, scoped to the state of Querétaro over a date window.

Each supertype is fed by several ``keywords.xlsx`` matching rules (one per
ontology *class*). This script gathers every enabled rule for a supertype's
classes, reuses ``get_entities_data.row_to_request`` to turn each rule into an
``elastic_client`` request (keywords / phrases / negatives / categories), ANDs
a Querétaro location filter and a date window onto it, and merges the hits into
a single deduped fixture under ``data/geo_qro_<supertype>/``.

Location filter: nested ``locations_mentioned.level_2_id == "48422"`` (the
state of Querétaro). This is the faithful nested-field filter — note it is *not*
the same as ``elastic_client``'s ``cvegeo`` request field, which does a flat
``location_mentioned_ids`` wildcard and does not match this id.

Window: defaults to the last 14 days (Mexico City), overridable via
``GEO_FIX_START`` / ``GEO_FIX_END`` (ISO ``YYYY-MM-DDTHH:MM``). The resolved
window is passed to ``elastic_client`` as a ``[start, end]`` period list, so the
date range is built by the same code path production uses.

Usage:
    ELASTIC_HOST=localhost ipython src/PoC/get_geo_event_fixtures.py
    GEO_FIX_SUPERTYPES=violence_event,protest_event \\
        ELASTIC_HOST=localhost python src/PoC/get_geo_event_fixtures.py
    GEO_FIX_START=2026-05-30T00:00 GEO_FIX_END=2026-06-13T23:59 \\
        ELASTIC_HOST=localhost python src/PoC/get_geo_event_fixtures.py

Env vars:
    GEO_FIX_SUPERTYPES  comma list of supertypes (default: the 6 geo-heavy ones)
    GEO_FIX_START       window start, ISO (default: GEO_FIX_END - 14 days)
    GEO_FIX_END         window end, ISO   (default: now, America/Mexico_City)
    GEO_FIX_STATE_ID    locations_mentioned.level_2_id value (default "48422")
    GEO_FIX_LIMIT       max hits per matching rule (default 2000)
"""

from __future__ import annotations

import datetime
import os
import sys
from pathlib import Path

import pandas as pd
import pytz
from dotenv import load_dotenv

_PROJECT_ROOT = Path("/Users/oscarcuellar/ocn/media/kg/kg/")
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

_ELASTIC_CLIENT_DIR = Path("/Users/oscarcuellar/ocn/media/elastic_client")
if _ELASTIC_CLIENT_DIR.exists() and str(_ELASTIC_CLIENT_DIR) not in sys.path:
    sys.path.insert(0, str(_ELASTIC_CLIENT_DIR))

load_dotenv(_PROJECT_ROOT / ".env.local")

from elasticsearch_dsl import Q  # noqa: E402

from elastic_client import SearchClient  # noqa: E402
from src.PoC.get_data import NEWS_FIELDS, save_docs  # noqa: E402
from src.PoC.get_entities_data import load_keywords, row_to_request  # noqa: E402


EVENT_TYPES_PATH = (
    _PROJECT_ROOT / "src" / "entities" / "extraction" / "catalogues" / "event_types.csv"
)

# The geo-heavy event supertypes selected for the large-scale linking test.
DEFAULT_SUPERTYPES = [
    "violence_event",
    "paid_mass_event",
    "emergency_event",
    "public_works_event",
    "public_infrastructure_event",
    "protest_event",
]

DEFAULT_STATE_ID = "48422"  # locations_mentioned.level_2_id for Querétaro
DEFAULT_WINDOW_DAYS = 14


def resolve_window() -> list[str]:
    """Return a ``[start, end]`` ISO period list (default: last 14 days)."""
    tz = pytz.timezone("America/Mexico_City")
    end_env = os.environ.get("GEO_FIX_END")
    start_env = os.environ.get("GEO_FIX_START")
    end = (
        datetime.datetime.fromisoformat(end_env)
        if end_env
        else datetime.datetime.now(tz).replace(second=0, microsecond=0, tzinfo=None)
    )
    start = (
        datetime.datetime.fromisoformat(start_env)
        if start_env
        else end - datetime.timedelta(days=DEFAULT_WINDOW_DAYS)
    )
    return [start.strftime("%Y-%m-%dT%H:%M"), end.strftime("%Y-%m-%dT%H:%M")]


def classes_for(supertype: str, event_types: pd.DataFrame) -> list[str]:
    return event_types[event_types["supertype"] == supertype]["event_type"].tolist()


def state_filter(state_id: str) -> Q:
    """Nested filter: doc mentions a location whose level_2_id == state_id."""
    return Q(
        "nested",
        path="locations_mentioned",
        query=Q("term", **{"locations_mentioned.level_2_id": state_id}),
    )


def fetch_supertype(
    rows: pd.DataFrame,
    period: list[str],
    geo_filter: Q,
    limit: int,
) -> list[dict]:
    """Build + run one query per matching rule; merge hits deduped by ES id."""
    client = SearchClient()
    docs_by_id: dict[str, dict] = {}
    for _, row in rows.iterrows():
        request = row_to_request(row)
        request["period"] = period          # 2-week list overrides the row period
        request.pop("cvegeo", None)          # we apply the nested level_2_id filter instead
        request.pop("location_type", None)
        search, echo = client.build_search(request)
        size = limit or echo.get("page_size") or 2000
        search = search.filter(geo_filter).source(NEWS_FIELDS).extra(from_=0, size=size)
        response = search.execute()
        for hit in response:
            doc = hit.to_dict()
            doc["_id"] = hit.meta.id
            docs_by_id.setdefault(doc["_id"], doc)
    return sorted(
        docs_by_id.values(),
        key=lambda d: str(d.get("date_created") or ""),
    )


def build_all() -> dict[str, int]:
    keywords = load_keywords()
    event_types = pd.read_csv(EVENT_TYPES_PATH)
    period = resolve_window()
    state_id = os.environ.get("GEO_FIX_STATE_ID", DEFAULT_STATE_ID)
    geo_filter = state_filter(state_id)
    limit = int(os.environ.get("GEO_FIX_LIMIT") or 2000)

    supertypes = [
        s.strip()
        for s in (os.environ.get("GEO_FIX_SUPERTYPES") or ",".join(DEFAULT_SUPERTYPES)).split(",")
        if s.strip()
    ]

    print(f"window: {period[0]} .. {period[1]}  |  level_2_id={state_id}")
    counts: dict[str, int] = {}
    for supertype in supertypes:
        classes = classes_for(supertype, event_types)
        rows = keywords[keywords["class"].astype(str).isin(classes)]
        docs = fetch_supertype(rows, period, geo_filter, limit)
        slug = f"geo_qro_{supertype}"
        save_docs(docs, _PROJECT_ROOT / "data" / slug, filename_stem=slug)
        counts[supertype] = len(docs)
        print(f"  {supertype:30s} rules={len(rows):2d}  docs={len(docs)}")
    return counts


if __name__ == "__main__":
    counts = build_all()
    print("\nfixture doc counts:")
    for supertype, n in counts.items():
        print(f"  {supertype:30s} {n}")
    print(f"  {'TOTAL (with overlap)':30s} {sum(counts.values())}")
