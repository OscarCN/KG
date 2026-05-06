"""
Run the entity linker on an extracted-records JSON file in a streaming
fashion, optionally tagging linked events with stances + claims.

Designed to be run step-by-step in IPython:
    ipython src/entities/linking/run_linking.py

Or interactively in a Jupyter/IPython session using %run:
    %run src/entities/linking/run_linking.py

After the script finishes, the following names are available for
inspection:

    records         — list of raw extracted records loaded from INPUT
    linker          — the EntityLinker instance
    linked          — dict {"events": [...]}  (themes/entities are skipped)
    stance_catalog  — StanceCatalog (only when TAGS_ENABLED)
    claim_catalogs  — ClaimCatalogRegistry (only when TAGS_ENABLED)
    stats           — StreamingStats (only when TAGS_ENABLED)

Set `TAGS_ENABLED = False` to bypass the tagging pipeline and reproduce
the original linker-only behaviour (same OUTPUT shape).
"""

from __future__ import annotations

import json
import logging
import re
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

# Verbose debug logging for the linking pipeline (LLM prompts, link decisions,
# per-event summary at the end). Other libraries stay at WARNING.
logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")
logging.getLogger("src.entities.linking").setLevel(logging.INFO)
logging.getLogger("src.entities.tags").setLevel(logging.INFO)

from src.entities.linking.link import EntityLinker, LinkResult

# ── Configuration ─────────────────────────────────────────────────────────────

INPUT: Path = _PROJECT_ROOT / "data" / "extracted_raw" / "ayuntamiento_tst.json"
OUTPUT: Path = _PROJECT_ROOT / "data" / "linked" / "ayuntamiento_tst.json"

# Set to False to skip geocoding (events will then be dropped for low precision).
GEOCODE: bool = True

# Tagging pipeline (Stage 1, in-memory). Disable to reproduce the
# original linker-only behaviour.
TAGS_ENABLED: bool = True
CUSTOMER_FIXTURE: Path = _PROJECT_ROOT / "data" / "tags" / "customer_75.json"

# Local news file with embedded `comments` arrays — used as the article
# corpus instead of hitting ES (Stage-1 testing). Set to `None` to fall
# back to ES via `Retrieval`.
NEWS_LOCAL: Optional[Path] = (
    _PROJECT_ROOT / "data" / "ayuntamiento_qro" / "ayuntamiento_qro_20260504_214928.json"
)

TAGS_OUTPUT: Optional[Path] = None  # set to a path for a final snapshot dump
SNAPSHOT_TOP_N: int = 10
BOOTSTRAP_CORPUS_LIMIT: int = 80
RUN_TIMESTAMP = datetime.now().strftime("%Y%m%d_%H%M%S")

# ── Robust JSON loader ────────────────────────────────────────────────────────

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


def _record_sort_key(r: Dict[str, Any]) -> str:
    # date_created may be missing; fall back to empty string (sorts first)
    return r.get("date_created") or ""


# Group by source_id, preserve publication order.
records_by_source: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
for r in records:
    sid = r.get("_source_id") or ""
    records_by_source[sid].append(r)

source_ids_in_order = sorted(
    records_by_source.keys(),
    key=lambda sid: min(_record_sort_key(r) for r in records_by_source[sid]),
)
print(f"  unique source_ids: {len(source_ids_in_order)}")


# ── Linker ────────────────────────────────────────────────────────────────────

linker = EntityLinker(geocode=GEOCODE)


# ── Tagging setup (optional) ──────────────────────────────────────────────────


def _setup_tagging():
    from src.entities.tags import (
        ClaimCatalogRegistry,
        InMemoryPersistence,
        StanceCatalog,
        load_customer_from_json,
    )
    from src.entities.tags.bootstrap import bootstrap_stance_catalog
    from src.entities.tags.claim_clusterer import ClaimClusterer
    from src.entities.tags.retrieval import LocalFileRetrieval, Retrieval
    from src.entities.tags.stance_adjudicator import StanceAdjudicator
    from src.entities.tags.stats import StreamingStats
    from src.entities.tags.tagging import TaggingOrchestrator

    cfg = load_customer_from_json(CUSTOMER_FIXTURE)
    customer = cfg.customer
    print(f"  customer: entity_id={customer.entity_id} name={customer.name!r}")

    if NEWS_LOCAL and NEWS_LOCAL.exists():
        retrieval = LocalFileRetrieval(NEWS_LOCAL)
        print(f"  retrieval: LocalFileRetrieval({NEWS_LOCAL.name})")
    else:
        retrieval = Retrieval()
        print("  retrieval: ES `news` index")

    stance_catalog = StanceCatalog(customer.entity_id)
    claim_catalogs = ClaimCatalogRegistry()
    persistence = InMemoryPersistence()
    stats = StreamingStats()

    return {
        "customer": customer,
        "retrieval": retrieval,
        "stance_catalog": stance_catalog,
        "claim_catalogs": claim_catalogs,
        "persistence": persistence,
        "stats": stats,
        "bootstrap_fn": bootstrap_stance_catalog,
        "TaggingOrchestrator": TaggingOrchestrator,
        "StanceAdjudicator": StanceAdjudicator,
        "ClaimClusterer": ClaimClusterer,
    }


tags_state: Optional[dict] = None
if TAGS_ENABLED:
    print("Setting up tagging pipeline …")
    try:
        tags_state = _setup_tagging()
    except Exception as ex:
        print(f"  tagging setup failed: {ex}")
        tags_state = None
        TAGS_ENABLED = False

# ── Bootstrap stance catalog (Phase 1) ────────────────────────────────────────

if TAGS_ENABLED and tags_state is not None:
    customer = tags_state["customer"]
    retrieval = tags_state["retrieval"]
    print("Bootstrapping stance catalog …")
    bootstrap_corpus = retrieval.get_customer_corpus(
        source_ids=source_ids_in_order, limit=BOOTSTRAP_CORPUS_LIMIT
    )
    print(f"  corpus items for bootstrap: {len(bootstrap_corpus)}")
    try:
        tags_state["stance_catalog"] = tags_state["bootstrap_fn"](
            customer, bootstrap_corpus
        )
        print(f"  bootstrap produced {len(tags_state['stance_catalog'].entries)} stances")
    except Exception as ex:
        print(f"  bootstrap failed: {ex} — continuing with empty catalog")


# ── Streaming loop ────────────────────────────────────────────────────────────


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


def _process_one_article(
    source_id: str,
    article_records: List[Dict[str, Any]],
    article_idx: int,
    total: int,
) -> None:
    # ── 1. Fetch article + comments from ES (or local file) ──────────────
    items = []
    if TAGS_ENABLED and tags_state is not None:
        retrieval = tags_state["retrieval"]
        article, comments = retrieval.get_article_with_comments(source_id)
        if article is not None:
            items.append(article)
        items.extend(comments)

    # ── 2. Stream each extracted record through the linker ───────────────
    article_link_results: List[tuple[Dict[str, Any], LinkResult]] = []
    for raw in article_records:
        lr = linker.link_one(raw)
        article_link_results.append((raw, lr))

    if TAGS_ENABLED and tags_state is not None:
        stats = tags_state["stats"]
        for _, lr in article_link_results:
            stats.on_link_result(lr.status)

        # ── 3. Tag once per linked event from this article ──────────────
        from src.entities.tags.apply import (
            apply_claim_phase,
            apply_stance_phase,
        )

        customer = tags_state["customer"]
        catalog = tags_state["stance_catalog"]
        registry = tags_state["claim_catalogs"]
        TaggingOrchestrator = tags_state["TaggingOrchestrator"]
        StanceAdjudicator = tags_state["StanceAdjudicator"]
        ClaimClusterer = tags_state["ClaimClusterer"]

        article_event_tags: List[str] = []
        for raw, lr in article_link_results:
            if lr.status not in ("created", "merged") or not lr.event_id or not items:
                continue
            ev_summary = _event_summary(lr.record or {})
            tagger = TaggingOrchestrator(customer, catalog)
            tagging_result = tagger.tag_batch(lr.event_id, ev_summary, items)

            adj_decisions = []
            if tagging_result.stance_proposals:
                adjudicator = StanceAdjudicator(customer, catalog)
                adj_decisions = adjudicator.adjudicate(
                    tagging_result.stance_proposals, items
                )

            stance_summary = apply_stance_phase(
                catalog,
                customer.entity_id,
                lr.event_id,
                tagging_result,
                adj_decisions,
            )
            stats.absorb_stance_apply(stance_summary)

            if tagging_result.claims:
                event_catalog = registry.get_or_create(customer.entity_id, lr.event_id)
                clusterer = ClaimClusterer(customer, event_catalog, ev_summary)
                clustering = clusterer.cluster(tagging_result.claims)
                claim_summary = apply_claim_phase(
                    registry,
                    customer.entity_id,
                    lr.event_id,
                    tagging_result.claims,
                    clustering,
                )
                stats.absorb_claim_apply(
                    claim_summary,
                    dropped_phase2=tagging_result.raw_claims_dropped_off_customer,
                )

            article_event_tags.append(f"{lr.status}:{lr.event_id}")

            # ── 4. Snapshot when a new event was created ─────────────
            if lr.status == "created":
                from src.entities.tags.stats import print_event_created_snapshot
                print_event_created_snapshot(
                    catalog, registry, lr.event_id, top_n=SNAPSHOT_TOP_N
                )

        stats.on_article()

        # ── 5. Per-article snapshot ─────────────────────────────────
        from src.entities.tags.stats import print_article_snapshot
        print_article_snapshot(
            stats,
            catalog,
            registry,
            article_idx=article_idx,
            article_total=total,
            source_id=source_id,
            article_event_ids=article_event_tags,
            top_n=SNAPSHOT_TOP_N,
        )
    else:
        # Linker-only mode — keep the legacy summary terse.
        n_created = sum(1 for _, lr in article_link_results if lr.status == "created")
        n_merged = sum(1 for _, lr in article_link_results if lr.status == "merged")
        if n_created or n_merged:
            print(
                f"[{article_idx}/{total}] {source_id} — created={n_created} merged={n_merged}"
            )


print()
print("Streaming articles …")
for i, source_id in enumerate(source_ids_in_order, start=1):
    _process_one_article(
        source_id=source_id,
        article_records=records_by_source[source_id],
        article_idx=i,
        total=len(source_ids_in_order),
    )

# ── Final summary ─────────────────────────────────────────────────────────────

linked = {"events": list(linker.events.values())}
n_in = len(records)
n_events = len(linked["events"])
print()
print(f"Linked: events={n_events}")
print(f"  input → output: {n_in} → {n_events}")
if linker.dropped:
    print(f"  dropped: {dict(linker.dropped)}")

OUTPUT.parent.mkdir(parents=True, exist_ok=True)
with open(OUTPUT, "w", encoding="utf-8") as f:
    json.dump(linked, f, ensure_ascii=False, indent=2, default=_json_default)
print(f"Wrote {OUTPUT}")

multi = sum(1 for r in linked["events"] if len(r.get("source_ids") or []) > 1)
print(f"  events merged from multiple sources: {multi}")

# ── Tagging artefacts ─────────────────────────────────────────────────────────

if TAGS_ENABLED and tags_state is not None:
    customer = tags_state["customer"]
    catalog = tags_state["stance_catalog"]
    registry = tags_state["claim_catalogs"]
    persistence = tags_state["persistence"]
    stats = tags_state["stats"]

    out_dir = _PROJECT_ROOT / "data" / "tags" / customer.slug
    snapshot_path = TAGS_OUTPUT or (out_dir / f"run_{RUN_TIMESTAMP}.json")
    persistence.save_snapshot(catalog, registry, snapshot_path)
    print(f"Wrote {snapshot_path}")

    print()
    print("Tagging summary:")
    for k, v in stats.__dict__.items():
        print(f"  {k}: {v}")

    # bind for IPython inspection
    stance_catalog = catalog
    claim_catalogs = registry
