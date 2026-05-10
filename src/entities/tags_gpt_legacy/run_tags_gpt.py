"""
Manual step-by-step runner for the decoupled tags_gpt pipeline.

Designed to be run in IPython, mirroring `src/entities/linking/run_linking.py`:

    ipython src/entities/tags_gpt/run_tags_gpt.py

or from an existing IPython/Jupyter session:

    %run src/entities/tags_gpt/run_tags_gpt.py

Run shape:

    1. Load extracted records and group them into source batches.
    2. Load customer config, LLM client, content retriever, stores, and steps.
    3. Bootstrap the customer stance catalog.
    4. Stream source batches:
          (a) retrieve article + comments,
          (b) link each extracted event,
          (c) stance tag/update per unique linked event,
          (d) claim tag/update per unique linked event.
    5. Write linked events/entities + tags_gpt snapshot.

After the script finishes, these names are bound for inspection:

    records          — extracted records loaded from INPUT
    batches          — SourceBatch values in streaming order
    customer         — Customer loaded from CUSTOMER_FIXTURE
    llm              — cached JsonLlm client
    retriever        — LocalJsonRetriever or EsNewsRetriever
    event_store      — EventStore with linked events used by tags_gpt
    stance_catalog   — StanceCatalog after the run
    claim_catalogs   — ClaimCatalogStore after the run
    pipeline         — StreamingTagsPipeline instance
    article_results  — ArticleProcessResult values, one per streamed source
    linked           — {"events": [...], "entities": [...]} JSON-compatible linked output
"""

from __future__ import annotations

import json
import logging
import sys
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any, Literal, Optional

from dotenv import load_dotenv

# Ensure project root is on sys.path. Keep the same setup style as
# `src/entities/linking/run_linking.py` so this file can be run directly.
_PROJECT_ROOT = Path("/Users/oscarcuellar/ocn/media/kg/kg/")
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

load_dotenv(_PROJECT_ROOT / ".env.local")

from src.entities.linking_gpt import TagsGptLinkingAdapter  # noqa: E402
from src.entities.tags_gpt import (  # noqa: E402
    ClaimCatalogStore,
    ClaimTagger,
    ClaimUpdater,
    ConsistencyPassStep,
    EsNewsRetriever,
    EventStore,
    LocalJsonRetriever,
    StanceBootstrapStep,
    StanceTagger,
    StanceUpdater,
    StreamingState,
    StreamingTagsPipeline,
    TypeTriageStep,
    default_cached_llm,
    group_by_source,
    load_content_graph,
    load_extracted_records,
    save_snapshot,
    sort_batches_by_publication,
)
from src.entities.tags_gpt.models import json_default  # noqa: E402


# ── Configuration ─────────────────────────────────────────────────────────────

INPUT: Path = _PROJECT_ROOT / "data" / "extracted_raw" / "ayuntamiento_tst.json"
OUTPUT: Path = _PROJECT_ROOT / "data" / "linked" / "tags_gpt_ayuntamiento_tst.json"
CUSTOMER_FIXTURE: Path = _PROJECT_ROOT / "data" / "tags" / "customer_75.json"

# Same local-news fixture convention as run_linking.py. Set to None to use ES.
NEWS_LOCAL: Optional[Path] = (
    _PROJECT_ROOT / "data" / "ayuntamiento_qro" / "ayuntamiento_qro_20260506_175946.json"
    # _PROJECT_ROOT / "data" / "ayuntamiento_qro" / "ayuntamiento_qro_20260504_214928.json"
)

BOOTSTRAP_CORPUS_LIMIT: int = 300
RUN_TIMESTAMP = datetime.now().strftime("%Y%m%d_%H%M%S")
TAGS_OUTPUT: Optional[Path] = None  # default: data/tags/<customer_slug>/tags_gpt_run_<ts>.json
GEOCODE: bool = True
TAGGING_STRATEGY: Literal["single_pass", "two_pass"] = "two_pass"
TRIAGE_COMMENT_BATCH_SIZE: int = 12
RUN_CONSISTENCY_PASS: bool = False
CONSISTENCY_SAMPLE_SIZE: int = 300

# Manual-run knobs.
LIMIT_BATCHES: Optional[int] = None
SOURCE_IDS: Optional[list[str]] = None
DEBUG_BOOTSTRAP: bool = True
DEBUG_LLM_IO: bool = True
STOP_BEFORE_SOURCE_ID: Optional[str] = (
    "https://www.facebook.com/permalink.php?"
    "story_fbid=pfbid02WWrzvgR84zxvAFnEE6u1hEKkhuTi9Rg9MuZiLPqExmRgCPqghNVz9t5yRUHYKrrLl"
    "&id=100064393401223"
)

# Manual report knobs.
REPORT_SAMPLE_ITEMS: int = 12
REPORT_SAMPLE_EVENTS: int = 8
REPORT_TOP_STANCES: int = 20
REPORT_TOP_SOURCE_EVENTS: int = 10
REPORT_TEXT_CHARS: int = 220


logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")
logging.getLogger().setLevel(logging.WARNING)
logging.getLogger("src.entities.tags_gpt").setLevel(logging.INFO)
logging.getLogger("src.entities.linking_gpt").setLevel(logging.INFO)
logging.getLogger("src.entities.tags_gpt.llm_io").setLevel(
    logging.DEBUG if DEBUG_LLM_IO else logging.WARNING
)

for _noisy_logger in (
    "elastic_transport",
    "elasticsearch",
    "httpcore",
    "httpx",
    "openai",
    "urllib3",
):
    logging.getLogger(_noisy_logger).setLevel(logging.WARNING)


def _write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2, default=json_default)


def _short_counts(counter: Counter) -> str:
    return ", ".join(f"{key}={value}" for key, value in counter.items()) or "(none)"


# ── 1. Load extracted records ─────────────────────────────────────────────────

print(f"Reading {INPUT}")
records = load_extracted_records(INPUT)
print(f"  loaded {len(records)} records")
print(f'  by supertype: {dict(Counter(r.get("_supertype", "?") for r in records))}')

batches = sort_batches_by_publication(group_by_source(records))
if SOURCE_IDS is not None:
    wanted = set(SOURCE_IDS)
    batches = [batch for batch in batches if batch.source_id in wanted]
if LIMIT_BATCHES is not None:
    batches = batches[:LIMIT_BATCHES]

source_ids_in_order = [batch.source_id for batch in batches]
print(f"  source batches: {len(batches)}")


# ── 2. Set up customer, retrieval, stores, and step objects ───────────────────

print()
print("Setting up tags_gpt pipeline ...")
graph = load_content_graph(CUSTOMER_FIXTURE)
customer = graph.customer
print(f"  customer: entity_id={customer.entity_id} name={customer.name!r}")

llm = default_cached_llm(_PROJECT_ROOT / "cache" / "tags_gpt")
print("  llm: cached OpenRouter JsonLlm")

if NEWS_LOCAL and NEWS_LOCAL.exists():
    retriever = LocalJsonRetriever(NEWS_LOCAL)
    print(f"  retrieval: LocalJsonRetriever({NEWS_LOCAL.name})")
else:
    retriever = EsNewsRetriever(cache_dir=_PROJECT_ROOT / "cache" / "tags_gpt_es_articles")
    print("  retrieval: ES `news` index")

event_store = EventStore()
claim_catalogs = ClaimCatalogStore()
linker = TagsGptLinkingAdapter(event_store=event_store, geocode=GEOCODE)
stance_updater = StanceUpdater(customer, llm)


# ── 3. Bootstrap stance catalog ───────────────────────────────────────────────

print()
print("Bootstrapping stance catalog ...")
bootstrap_corpus = retriever.get_customer_corpus(
    source_ids_in_order,
    limit=BOOTSTRAP_CORPUS_LIMIT,
)
print(f"  corpus items: {len(bootstrap_corpus)}")

bootstrap_payload = {}
bootstrap_prompt_text = ""
bootstrap_response = {}
bootstrap_items = []
bootstrap_debug = None
bootstrap_catalog_results = []
if DEBUG_BOOTSTRAP:
    bootstrap_step = StanceBootstrapStep(llm)
    bootstrap_debug = bootstrap_step.bootstrap_with_debug(customer, bootstrap_corpus)
    stance_catalog = bootstrap_debug.catalog
    bootstrap_items = bootstrap_debug.items
    bootstrap_catalog_results = bootstrap_debug.catalog_results
    bootstrap_triage_calls = bootstrap_debug.triage.calls if bootstrap_debug.triage else []
    if bootstrap_triage_calls:
        bootstrap_payload = bootstrap_triage_calls[0].payload
        bootstrap_prompt_text = bootstrap_triage_calls[0].prompt
        bootstrap_response = bootstrap_triage_calls[0].response

    print(f"  bootstrap triage batches: {len(bootstrap_triage_calls)}")
    for triage_call in bootstrap_triage_calls:
        print(
            f"    batch {triage_call.batch_index}: "
            f"triaged={len(triage_call.result.triaged)} "
            f"dropped_invalid={triage_call.result.dropped_invalid}"
        )

    print(f"  bootstrap catalog calls: {len(bootstrap_catalog_results)}")
    for catalog_result in bootstrap_catalog_results:
        if catalog_result.skipped:
            print(f"    {catalog_result.stance_type}: skipped no triaged items")
        else:
            print(
                f"    {catalog_result.stance_type}: "
                f"created={catalog_result.created} "
                f"dropped_invalid={catalog_result.dropped_invalid} "
                f"dropped_insufficient_evidence={catalog_result.dropped_insufficient_evidence}"
            )
else:
    stance_catalog = StanceBootstrapStep(llm).bootstrap(customer, bootstrap_corpus)
print(f"  produced {len(stance_catalog.entries)} stance entries")
print(f"  by type: {dict(Counter(entry.primary_type for entry in stance_catalog.entries.values()))}")


# ── 4. Build streaming coordinator ────────────────────────────────────────────

state = StreamingState(
    event_store=event_store,
    stance_catalog=stance_catalog,
    claim_catalogs=claim_catalogs,
    tagging_strategy=TAGGING_STRATEGY,
)

pipeline = StreamingTagsPipeline(
    state=state,
    retriever=retriever,
    linker=linker,
    stance_tagger=StanceTagger(customer, llm),
    stance_updater=stance_updater,
    claim_tagger=ClaimTagger(customer, llm),
    claim_updater=ClaimUpdater(customer, llm),
    type_triage_step=TypeTriageStep(customer, llm),
    triage_comment_batch_size=TRIAGE_COMMENT_BATCH_SIZE,
)


# ── 5. Stream source batches ──────────────────────────────────────────────────

print()
print("Streaming batches ...")
article_results = []
link_status_counts: Counter[str] = Counter()
manual_batch = None
manual_batch_index = None
manual_bundle = None

for i, batch in enumerate(batches, start=1):
    print(f"[{i}/{len(batches)}] {batch.source_id}")
    if STOP_BEFORE_SOURCE_ID and batch.source_id == STOP_BEFORE_SOURCE_ID:
        manual_batch = batch
        manual_batch_index = i
        manual_bundle = retriever.get_article_bundle(batch.source_id)
        print("      DEBUG STOP: matched STOP_BEFORE_SOURCE_ID")
        print("      Automatic streaming stops before processing this batch.")
        print("      Bound for manual use: manual_batch, manual_batch_index, manual_bundle, pipeline")
        print("      Example: result = pipeline.process_batch(manual_batch)")
        break

    result = pipeline.process_batch(batch)
    article_results.append(result)

    batch_status_counts = Counter(link.status for link in result.link_results)
    link_status_counts.update(batch_status_counts)

    stance_counts = Counter()
    claim_counts = Counter()
    for event_result in result.event_tag_results:
        stance_counts.update(event_result.stance_update.counts)
        claim_counts.update(event_result.claim_update.counts)

    print(f"      link: {_short_counts(batch_status_counts)}")
    print(f"      stance_update: {_short_counts(stance_counts)}")
    print(f"      claim_update: {_short_counts(claim_counts)}")


if manual_batch is not None:
    print()
    print("Manual stop reached; downstream outputs/reports are partial up to the prior batch.")


consistency_result = None
if RUN_CONSISTENCY_PASS:
    print()
    print("Running stance consistency pass ...")
    consistency_result = ConsistencyPassStep(
        customer,
        llm,
        stance_updater,
        sample_size=CONSISTENCY_SAMPLE_SIZE,
    ).run(
        stance_catalog,
        items_seen=state.items_seen,
        claim_catalogs=claim_catalogs,
    )
    print(f"  consistency: {_short_counts(Counter(consistency_result.summary.counts))}")


# ── 6. Write outputs and print summary ────────────────────────────────────────

linked = {
    "events": list(linker.linker.events.values()),
    "entities": list(linker.linker.entities.values()),
}
_write_json(OUTPUT, linked)
print()
print(f"Wrote {OUTPUT}")
print(f"  linked events: {len(linked['events'])}")
print(f"  linked entities: {len(linked['entities'])}")
print(f"  link statuses: {_short_counts(link_status_counts)}")

snapshot_path = TAGS_OUTPUT or (
    _PROJECT_ROOT / "data" / "tags" / customer.slug / f"tags_gpt_run_{RUN_TIMESTAMP}.json"
)
save_snapshot(
    snapshot_path,
    event_store=event_store,
    stance_catalog=stance_catalog,
    claim_catalogs=claim_catalogs,
)
print(f"Wrote {snapshot_path}")

print()
print("Tagging summary:")
print(f"  stance entries: {len(stance_catalog.entries)}")
print(f"  stance assignments: {len(stance_catalog.assignments)}")
print(f"  claim catalogs: {len(list(claim_catalogs.values()))}")
print(f"  claim clusters: {sum(len(catalog.clusters) for catalog in claim_catalogs.values())}")
print(f"  source items seen: {len(state.items_seen)}")


print()
print("Inspection report:")


def _short(text: str, limit: int = REPORT_TEXT_CHARS) -> str:
    value = " ".join(str(text or "").split())
    return value if len(value) <= limit else f"{value[:limit].rstrip()}..."


def _stance_label(assignment) -> str:
    entry = stance_catalog.entries.get(assignment.stance_id or "")
    retired = stance_catalog.retired_entries.get(assignment.stance_id or "")
    if entry:
        return entry.label
    if retired:
        return f"{retired.label} [retired]"
    return f"<{assignment.stance_type}:uncatalogued>"


stances_by_item = {}
for assignment in stance_catalog.assignments:
    stances_by_item.setdefault(assignment.source_item_id, []).append(assignment)

claims_by_item = {}
claim_catalog_by_event = {catalog.event_id: catalog for catalog in claim_catalogs.values()}
for catalog in claim_catalogs.values():
    for cluster in catalog.clusters.values():
        for claim in cluster.members:
            claims_by_item.setdefault(claim.source_item_id, []).append(
                {
                    "event_id": catalog.event_id,
                    "canonical": cluster.canonical,
                    "verbatim": _short(claim.verbatim, 140),
                    "importance": claim.importance,
                }
            )

event_rows = []
for event in event_store.values():
    catalog = claim_catalog_by_event.get(event.id)
    n_claims = sum(len(cluster.members) for cluster in catalog.clusters.values()) if catalog else 0
    event_rows.append((n_claims, len(event.source_ids), event, catalog))

tagged_items = []
for index, item in enumerate(state.items_seen.values()):
    activity = len(stances_by_item.get(item.id, [])) + len(claims_by_item.get(item.id, []))
    if activity:
        kind_rank = {"user_post": 0, "user_comment": 1, "article": 2}.get(item.kind, 3)
        tagged_items.append((kind_rank, -activity, index, item))

by_type = Counter(assignment.stance_type for assignment in stance_catalog.assignments)
by_label = Counter(_stance_label(assignment) for assignment in stance_catalog.assignments)
by_event_label = Counter(
    (assignment.event_id, _stance_label(assignment))
    for assignment in stance_catalog.assignments
    if assignment.event_id
)

report = {
    "sample_items": [
        {
            "kind": item.kind,
            "id": _short(item.id, 86),
            "text": _short(item.text),
            "stances": [
                {
                    "type": assignment.stance_type,
                    "label": _stance_label(assignment),
                    "sentiment": assignment.sentiment,
                    "relevance": assignment.consistency_relevance,
                }
                for assignment in stances_by_item.get(item.id, [])[:6]
            ],
            "claims": claims_by_item.get(item.id, [])[:5],
        }
        for _, _, _, item in sorted(tagged_items)[:REPORT_SAMPLE_ITEMS]
    ],
    "events_with_claims": [
        {
            "id": event.id,
            "event_type": event.event_type,
            "description": _short(event.description or event.name, 120),
            "sources": [_short(source_id, 86) for source_id in event.source_ids[:6]],
            "source_count": len(event.source_ids),
            "claim_groups": [] if not catalog else [
                {
                    "canonical": cluster.canonical,
                    "n": len(cluster.members),
                    "importance_max": cluster.importance_max,
                    "samples": [_short(member.verbatim, 150) for member in cluster.members[:3]],
                }
                for cluster in sorted(
                    catalog.clusters.values(),
                    key=lambda item: (item.importance_max, len(item.members)),
                    reverse=True,
                )
            ],
        }
        for _, _, event, catalog in sorted(
            event_rows,
            key=lambda row: (row[0], row[1]),
            reverse=True,
        )[:REPORT_SAMPLE_EVENTS]
    ],
    "stance_aggregates": {
        "by_type": dict(by_type),
        "top_labels": [{"label": label, "count": count} for label, count in by_label.most_common(REPORT_TOP_STANCES)],
        "top_event_stance_pairs": [
            {
                "event_id": event_id,
                "event": _short((event_store.get(event_id).description or event_store.get(event_id).name), 100)
                if event_store.get(event_id)
                else event_id,
                "label": label,
                "count": count,
            }
            for (event_id, label), count in by_event_label.most_common(10)
        ],
    },
    "events_with_most_sources": [
        {
            "id": event.id,
            "event_type": event.event_type,
            "description": _short(event.description or event.name, 140),
            "source_count": len(event.source_ids),
        }
        for event in sorted(
            event_store.values(),
            key=lambda item: len(item.source_ids),
            reverse=True,
        )[:REPORT_TOP_SOURCE_EVENTS]
    ],
}
print(json.dumps(report, ensure_ascii=False, indent=2, default=json_default))
