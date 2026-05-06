"""
Stream extracted records through the linker and the tags pipeline.

Designed to be run step-by-step in IPython:
    ipython src/entities/linking/run_linking.py
or, inside an existing IPython session:
    %run src/entities/linking/run_linking.py

Run shape:

    1.  Load extracted records, group by `_source_id`, sort by publication date.
    2.  Set up the customer fixture, retrieval, linker, catalogs, persistence.
    3.  Phase 1 — bootstrap the stance catalog from a slice of the corpus.
    4.  Stream articles. For each article:
            (a) fetch the article + embedded comments,
            (b) link each extracted record (`link_one`),
            (c) tag each unique linked (article, event) pair (Phase 2/3/4/5),
            (d) print incremental snapshots.
    5.  Write linked events + a tagging snapshot, print summary blocks.

After the script finishes, these names are bound for inspection:

    records         — list of raw extracted records loaded from INPUT
    linker          — the EntityLinker instance
    linked          — dict {"events": [...]}  (themes/entities are skipped)
    customer        — Customer dataclass loaded from CUSTOMER_FIXTURE
    stance_catalog  — StanceCatalog after the run
    claim_catalogs  — ClaimCatalogRegistry after the run
    items_seen      — {source_item_id → SourceItem} captured during streaming
    stats           — StreamingStats counters
"""

from __future__ import annotations

import json
import logging
import sys
from collections import Counter, defaultdict
from datetime import date, datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from dotenv import load_dotenv

# Ensure project root is on sys.path
_PROJECT_ROOT = Path("/Users/oscarcuellar/ocn/media/kg/kg/")
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

# Load OPENROUTER_API_KEY etc. from .env.local at the project root.
load_dotenv(_PROJECT_ROOT / ".env.local")

logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")
logging.getLogger("src.entities.linking").setLevel(logging.INFO)
logging.getLogger("src.entities.tags").setLevel(logging.INFO)

from src.entities.linking.link import EntityLinker, LinkResult
from src.entities.tags import (
    ClaimCatalogRegistry,
    InMemoryPersistence,
    SourceItem,
    StanceCatalog,
    load_customer_from_json,
)
from src.entities.tags.apply import apply_claim_phase, apply_stance_phase
from src.entities.tags.bootstrap import bootstrap_stance_catalog
from src.entities.tags.claim_clusterer import ClaimClusterer
from src.entities.tags.retrieval import LocalFileRetrieval, Retrieval
from src.entities.tags.stance_adjudicator import StanceAdjudicator
from src.entities.tags.stats import (
    StreamingStats,
    print_article_snapshot,
    print_event_created_snapshot,
    print_sample_source_items,
    print_top_events,
)
from src.entities.tags.tagging import TaggingOrchestrator

# ── Configuration ─────────────────────────────────────────────────────────────

INPUT: Path = _PROJECT_ROOT / "data" / "extracted_raw" / "ayuntamiento_tst.json"
OUTPUT: Path = _PROJECT_ROOT / "data" / "linked" / "ayuntamiento_tst.json"
CUSTOMER_FIXTURE: Path = _PROJECT_ROOT / "data" / "tags" / "customer_75.json"

# Local news file with embedded `comments` arrays — used as the article
# corpus instead of hitting ES (Stage-1 testing). Set to `None` to fall
# back to ES via `Retrieval`.
NEWS_LOCAL: Optional[Path] = (
    _PROJECT_ROOT / "data" / "ayuntamiento_qro" / "ayuntamiento_qro_20260506_015754.json"
    # _PROJECT_ROOT / "data" / "ayuntamiento_qro" / "ayuntamiento_qro_20260504_214928.json"
)

# Set to False to skip geocoding (events will then be dropped for low precision).
GEOCODE: bool = True

SNAPSHOT_TOP_N: int = 10
BOOTSTRAP_CORPUS_LIMIT: int = 80
RUN_TIMESTAMP = datetime.now().strftime("%Y%m%d_%H%M%S")
TAGS_OUTPUT: Optional[Path] = None  # explicit override; defaults to data/tags/<slug>/run_<ts>.json


def _json_default(value: Any) -> Any:
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, set):
        return sorted(value)
    raise TypeError(f"Object of type {type(value).__name__} is not JSON serializable")


def _event_summary(event_record: Dict[str, Any]) -> str:
    pieces = [
        f"event_type: {event_record.get('event_type')}",
        f"name: {event_record.get('name')}",
        f"description: {(event_record.get('description') or '')[:600]}",
    ]
    loc = event_record.get("location") or {}
    if loc:
        pieces.append(f"location: {json.dumps(loc, ensure_ascii=False)}")
    dr = (event_record.get("date_range") or {}).get("date_range") or {}
    if dr:
        pieces.append(f"date_range: {json.dumps(dr, ensure_ascii=False, default=_json_default)}")
    return "\n".join(pieces)


# ── 1. Load extracted records ─────────────────────────────────────────────────

print(f"Reading {INPUT}")
records: List[Dict[str, Any]] = json.loads(INPUT.read_text(encoding="utf-8"))

print(f"  loaded {len(records)} records")
print(f'  by supertype: {dict(Counter(r.get("_supertype", "?") for r in records))}')

records_by_source: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
for r in records:
    records_by_source[r.get("_source_id") or ""].append(r)

source_ids_in_order = sorted(
    records_by_source.keys(),
    key=lambda sid: min(r.get("date_created") or "" for r in records_by_source[sid]),
)
print(f"  unique source_ids: {len(source_ids_in_order)}")

# TEST SAMPLE RECORDS
source_ids_in_order = [
    t for t in source_ids_in_order
    if any(e.get("event_type") == "water_issue" for e in records_by_source[t])
]

source_ids_in_order = sorted(
    records_by_source.keys(),
    key=lambda sid: min(r.get("date_created") or "" for r in records_by_source[sid]),
)


# ── 2. Set up customer, retrieval, linker, catalogs ───────────────────────────

print()
print("Setting up tagging pipeline …")
customer = load_customer_from_json(CUSTOMER_FIXTURE).customer
print(f"  customer: entity_id={customer.entity_id} name={customer.name!r}")

if NEWS_LOCAL and NEWS_LOCAL.exists():
    retrieval = LocalFileRetrieval(NEWS_LOCAL)
    print(f"  retrieval: LocalFileRetrieval({NEWS_LOCAL.name})")
else:
    retrieval = Retrieval()
    print("  retrieval: ES `news` index")

linker = EntityLinker(geocode=GEOCODE)
stance_catalog = StanceCatalog(customer.entity_id)
claim_catalogs = ClaimCatalogRegistry()
persistence = InMemoryPersistence()
stats = StreamingStats()


# ── 3. Phase 1 — bootstrap the stance catalog ─────────────────────────────────

print()
print("Bootstrapping stance catalog (Phase 1) …")
bootstrap_corpus = retrieval.get_customer_corpus(
    source_ids=source_ids_in_order, limit=BOOTSTRAP_CORPUS_LIMIT
)
print(f"  corpus items: {len(bootstrap_corpus)}")
stance_catalog = bootstrap_stance_catalog(customer, bootstrap_corpus)
print(f"  produced {len(stance_catalog.entries)} stance entries")


# ── Per-event tagging helper (Phases 2 → 3 → 4 → 5) ───────────────────────────


def tag_event(event_id: str, event_record: Dict[str, Any], items: List[SourceItem]) -> None:
    """Run Phases 2-5 for one (article-items, linked event) pair.

    Phase 2 — tag stances + claims (one LLM call producing both).
    Phase 3 — adjudicate stance proposals (separate LLM, only if any).
    Phase 4 — cluster raw claims (per-event LLM, only if any claims).
    Phase 5 — apply tagging + adjudications + clustering into the catalogs.
    """
    if not items:
        return
    summary = _event_summary(event_record)

    # Phase 2 — tagging
    tagging = TaggingOrchestrator(customer, stance_catalog).tag_batch(event_id, summary, items)

    # Phase 3 — adjudication of any proposed catalog mutations
    adjudications = []
    if tagging.stance_proposals:
        adjudications = StanceAdjudicator(customer, stance_catalog).adjudicate(
            tagging.stance_proposals, items
        )

    # Phase 5a — apply stance side
    stance_apply_summary = apply_stance_phase(
        stance_catalog, customer.entity_id, event_id, tagging, adjudications,
    )
    stats.absorb_stance_apply(stance_apply_summary)

    # Phase 4 + 5b — cluster + apply claim side (only if there are claims)
    if tagging.claims:
        event_claim_catalog = claim_catalogs.get_or_create(customer.entity_id, event_id)
        clustering = ClaimClusterer(customer, event_claim_catalog, summary).cluster(tagging.claims)
        claim_apply_summary = apply_claim_phase(
            claim_catalogs, customer.entity_id, event_id, tagging.claims, clustering,
        )
        stats.absorb_claim_apply(
            claim_apply_summary,
            dropped_phase2=tagging.raw_claims_dropped_off_customer,
        )


# ── 4. Streaming loop ─────────────────────────────────────────────────────────

items_seen: Dict[str, SourceItem] = {}

print()
print("Streaming articles …")
for i, source_id in enumerate(source_ids_in_order, start=1):
    print(f"[{i}/{len(source_ids_in_order)}] {source_id}")
    article_records = records_by_source[source_id]

    # 4a. Fetch the article + its embedded comments
    article, comments = retrieval.get_article_with_comments(source_id)
    items: List[SourceItem] = ([article] if article else []) + comments
    for it in items:
        items_seen[it.id] = it

    # 4b. Link each extracted record (one at a time, streaming)
    link_results = [linker.link_one(raw) for raw in article_records]
    for lr in link_results:
        stats.on_link_result(lr.status)

    # 4c. Tag each unique linked event from this article exactly once.
    #     Multiple extracted records often land on the same canonical event;
    #     we don't want to tag the (article, event) pair multiple times.
    unique_event_results: Dict[str, LinkResult] = {}
    for lr in link_results:
        if lr.status in ("created", "merged") and lr.event_id:
            unique_event_results.setdefault(lr.event_id, lr)

    for lr in unique_event_results.values():
        tag_event(lr.event_id, lr.record or {}, items)
        if lr.status == "created":
            print_event_created_snapshot(
                stance_catalog, claim_catalogs, lr.event_id, top_n=SNAPSHOT_TOP_N,
            )

    # 4d. Per-article metrics block
    stats.on_article()
    print_article_snapshot(
        stats,
        stance_catalog,
        claim_catalogs,
        article_event_ids=[f"{lr.status}:{lr.event_id}" for lr in unique_event_results.values()],
        top_n=SNAPSHOT_TOP_N,
    )


# ── 5. Persist and print final summary ────────────────────────────────────────

linked = {"events": list(linker.events.values())}
print()
print(f"Linked: events={len(linked['events'])}")
print(f"  input → output: {len(records)} → {len(linked['events'])}")
if linker.dropped:
    print(f"  dropped: {dict(linker.dropped)}")

OUTPUT.parent.mkdir(parents=True, exist_ok=True)
with open(OUTPUT, "w", encoding="utf-8") as f:
    json.dump(linked, f, ensure_ascii=False, indent=2, default=_json_default)
print(f"Wrote {OUTPUT}")
print(f"  events merged from multiple sources: "
      f"{sum(1 for r in linked['events'] if len(r.get('source_ids') or []) > 1)}")

snapshot_path = TAGS_OUTPUT or (
    _PROJECT_ROOT / "data" / "tags" / customer.slug / f"run_{RUN_TIMESTAMP}.json"
)
persistence.save_snapshot(stance_catalog, claim_catalogs, snapshot_path)
print(f"Wrote {snapshot_path}")

print()
print("Tagging summary:")
for k, v in stats.__dict__.items():
    print(f"  {k}: {v}")

print_sample_source_items(stance_catalog, claim_catalogs, items_seen, n=3)
print_top_events(
    linked["events"],
    stance_catalog,
    claim_catalogs,
    items_seen,
    customer.entity_id,
    n_events=5,
    items_per_event=3,
)
