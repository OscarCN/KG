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

logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")
logging.getLogger("src.entities.tags_gpt").setLevel(logging.INFO)
logging.getLogger("src.entities.linking_gpt").setLevel(logging.INFO)

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

BOOTSTRAP_CORPUS_LIMIT: int = 80
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

stance_catalog = StanceBootstrapStep(llm).bootstrap(customer, bootstrap_corpus)
print(f"  produced {len(stance_catalog.entries)} stance entries")


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

for i, batch in enumerate(batches, start=1):
    print(f"[{i}/{len(batches)}] {batch.source_id}")
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
