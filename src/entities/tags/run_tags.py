"""Stream a pre-linked corpus through the tags pipeline, step-by-step.

Designed to be run in IPython:
    ipython src/entities/tags/run_tags.py
or, inside a session:
    %run src/entities/tags/run_tags.py

After it finishes, every step instance and every catalog is bound at
module scope so you can poke around:

    customer            — Customer dataclass loaded from CUSTOMER_FIXTURE
    retriever           — ArticleBundleRetriever over the pre-linked fixture
    bundles             — list of ArticleBundle
    triage_step         — TypeTriageStep
    stance_tagger       — StanceTagger
    stance_updater      — StanceUpdater
    claim_tagger        — ClaimTagger
    claim_updater       — ClaimUpdater
    bootstrap_step      — BootstrapStep
    consistency_step    — ConsistencyPassStep
    state               — StreamingState (.stance_catalog, .claim_catalogs, .items_seen)
    pipeline            — StreamingTagsPipeline
    stats               — StreamingStats counters

To re-tag a single bundle after the run:
    result = pipeline.process_bundle(bundles[0])
"""

from __future__ import annotations

import logging
import sys
from datetime import datetime
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv

# Ensure the project root is on sys.path so `src.*` imports resolve when
# this file is run directly (`ipython src/entities/tags/run_tags.py`).
_PROJECT_ROOT = Path('/Users/oscarcuellar/ocn/media/kg/kg/src/entities/tags/run_tags.py').resolve().parents[3]
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

load_dotenv(_PROJECT_ROOT / ".env.local")

logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")
logging.getLogger("src.entities.tags").setLevel(logging.INFO)

# Per-phase prompt+response loggers. Set one (or several) to DEBUG to dump
# the full payload for that phase. Uncomment whatever you want to debug.

# logging.getLogger("tags.prompts").setLevel(logging.DEBUG)             # all six at once
# logging.getLogger("tags.prompts.bootstrap").setLevel(logging.DEBUG)   # BootstrapStep
# logging.getLogger("tags.prompts.triage").setLevel(logging.DEBUG)      # TypeTriageStep
# logging.getLogger("tags.prompts.tagging").setLevel(logging.DEBUG)     # StanceTagger (tag_per_type)
logging.getLogger("tags.prompts.claim_tag").setLevel(logging.DEBUG)   # ClaimTagger (extraction)
logging.getLogger("tags.prompts.claim_group").setLevel(logging.DEBUG) # ClaimUpdater (grouping)
logging.getLogger("tags.prompts.consistency").setLevel(logging.DEBUG) # ConsistencyPassStep

from src.entities.tags.bootstrap import BootstrapStep
from src.entities.tags.catalogs import ClaimCatalogStore, StanceCatalog
from src.entities.tags.consistency import ConsistencyPassStep
from src.entities.tags.llm import (
    CachedJsonLlm,
    LoggingJsonLlm,
    OpenRouterJsonLlm,
    cache_dir_for,
)
from src.entities.tags.persistence import (
    load_snapshot,
    load_stance_catalog,
    save_snapshot,
    save_stance_catalog,
)
from src.entities.tags.retrieval import ArticleBundleRetriever
from src.entities.tags.runner import LocalRunConfig, load_customer
from src.entities.tags.loop_helpers import (
    per_entry_counts,
    print_bundle_progress,
    print_catalogs_summary,
    print_corpus_accounting,
    run_consistency_pass_at_bundle,
)
from src.entities.tags.stats import (
    StreamingStats,
    print_catalog_overview,
    print_sample_source_items,
    print_top_events,
    print_top_stances_by_type,
)
from src.entities.tags.streaming import StreamingState, StreamingTagsPipeline
from src.entities.tags.tagging import (
    ClaimTagger,
    ClaimUpdater,
    StanceTagger,
    StanceUpdater,
)
from src.entities.tags.triage import TypeTriageStep


# ── Configuration ─────────────────────────────────────────────────────────────

CUSTOMER_FIXTURE: Path = _PROJECT_ROOT / "data" / "tags" / "customer_75.json"

# Pre-linked fixture (output of `scripts/build_linked_fixture.py`). The
# sibling event store is auto-derived as `<stem>__events.json`.
LINKED_FIXTURE: Path = (
    _PROJECT_ROOT
    / "data"
    / "linked"
    / "ayuntamiento_qro_20260506_175946.json"
)
EVENTS_FIXTURE: Path = LINKED_FIXTURE.with_name(f"{LINKED_FIXTURE.stem}__events.json")

# Where to write the bootstrap catalog + run snapshot + consistency-pass result.
TAGS_OUTPUT_DIR: Path = _PROJECT_ROOT / "data" / "tags"
RUN_TIMESTAMP: str = datetime.now().strftime("%Y%m%d_%H%M%S")

# Toggles
BOOTSTRAP_IF_MISSING: bool = True   # run Phase 1 if no catalog file exists
RUN_STREAMING: bool = True          # run the per-bundle streaming loop
RUN_CONSISTENCY: bool = False       # run consistency pass at the end

# Streaming knobs
INCLUDE_COMMENTS_IN_CLAIMS: bool = False
SNAPSHOT_TOP_N: int = 10
BUNDLE_LIMIT: Optional[int] = None  # cap to first N bundles for fast iteration
BOOTSTRAP_BUNDLE_LIMIT: int = 70    # how many bundles the bootstrap LLM call sees
CATALOG_SUMMARY_EVERY: Optional[int] = 40   # print stance + claim summary every N bundles (None = off)

# Consistency-pass knobs
CONSISTENCY_AT_BUNDLE: Optional[int] = 120   # trigger mid-stream consistency at this bundle index (None = off)


# ── 0. Build a `LocalRunConfig` (used for env-var-driven model defaults) ──────

config = LocalRunConfig(
    customer_path=CUSTOMER_FIXTURE,
    linked_path=LINKED_FIXTURE,
    events_path=EVENTS_FIXTURE,
    output_dir=TAGS_OUTPUT_DIR,
    catalog_path=None,
    include_comments=INCLUDE_COMMENTS_IN_CLAIMS,
    snapshot_top_n=SNAPSHOT_TOP_N,
)
print(f"models: triage={config.triage_model}")
print(f"        bootstrap={config.bootstrap_model}")
print(f"        tagger={config.tagger_model}")
print(f"        claim_tagger={config.claim_tagger_model}")
print(f"        claim_updater={config.claim_updater_model}")
print(f"        consistency={config.consistency_model}")


# ── 1. Load customer + open the pre-linked fixture ────────────────────────────

print()
print("Loading customer + pre-linked fixture …")
customer = load_customer(CUSTOMER_FIXTURE)
print(f"  customer: entity_id={customer.entity_id} name={customer.name!r}")

retriever = ArticleBundleRetriever(LINKED_FIXTURE, EVENTS_FIXTURE, customer=customer)
bundles: list = []
if not LINKED_FIXTURE.exists():
    print(f"  ⚠️  LINKED_FIXTURE not found: {LINKED_FIXTURE}")
    print(f"      Run scripts/build_linked_fixture.py first to produce it.")
    print(f"      Continuing with empty bundles list — Phase 1 / 4 / 6 will be skipped.")
else:
    bundles = list(retriever.iter_bundles())
    if BUNDLE_LIMIT:
        bundles = bundles[:BUNDLE_LIMIT]
    n_with_events = sum(1 for b in bundles if b.event_ids)
    print(f"  bundles loaded: {len(bundles)} ({n_with_events} with linked events)")
    print(f"  events resolved: {len(retriever.event_descriptions())}")


# ── 2. Build LLM adapters (one cached client per phase) ───────────────────────


def _llm(phase: str, model: str) -> LoggingJsonLlm:
    """Outer → inner: LoggingJsonLlm → CachedJsonLlm → OpenRouterJsonLlm.

    Per-phase logger names follow `tags.prompts.<phase>`; set one to DEBUG
    to dump that phase's prompts and responses (the parent
    `tags.prompts` enables all six).
    """
    return LoggingJsonLlm(
        CachedJsonLlm(
            OpenRouterJsonLlm(model=model),
            cache_dir=cache_dir_for(phase, customer.entity_id),
            model=model,
            extra={"phase": phase},
        ),
        phase=phase,
    )


triage_llm = _llm("triage", config.triage_model)
bootstrap_llm = _llm("bootstrap", config.bootstrap_model)
tagger_llm = _llm("tagging", config.tagger_model)
claim_tagger_llm = _llm("claim_tag", config.claim_tagger_model)
claim_updater_llm = _llm("claim_group", config.claim_updater_model)
consistency_llm = _llm("consistency", config.consistency_model)


# ── 3. Build step instances (one of each, reusable across the run) ───────────

triage_step = TypeTriageStep(customer, triage_llm)
stance_tagger = StanceTagger(customer, tagger_llm)
stance_updater = StanceUpdater()
claim_tagger = ClaimTagger(customer, claim_tagger_llm, include_comments=INCLUDE_COMMENTS_IN_CLAIMS)
claim_updater = ClaimUpdater(customer, claim_updater_llm)
bootstrap_step = BootstrapStep(customer, triage_step, bootstrap_llm)
consistency_step = ConsistencyPassStep(
    customer,
    consistency_llm,
    bootstrap_step=bootstrap_step,
)


# ── 4. Phase 1 — bootstrap the typed stance catalog ──────────────────────────

bootstrap_path: Path = TAGS_OUTPUT_DIR / customer.slug / "bootstrap.json"
stance_catalog: StanceCatalog
bootstrapped_now: bool = False             # whether Phase 1 LLM ran THIS session
bootstrap_window_covered: bool = False     # whether bundles[:BOOTSTRAP_BUNDLE_LIMIT] are already tagged

if bootstrap_path.exists():
    stance_catalog = load_stance_catalog(bootstrap_path)
    bootstrap_window_covered = True
    print()
    print(f"Loaded existing bootstrap catalog from {bootstrap_path} "
          f"({len(stance_catalog.entries)} entries, "
          f"{len(stance_catalog.assignments)} assignments)")
elif BOOTSTRAP_IF_MISSING and bundles:
    bootstrap_bundles = bundles[:BOOTSTRAP_BUNDLE_LIMIT]
    print()
    print(f"Bootstrapping stance catalog (Phase 1) — using "
          f"{len(bootstrap_bundles)} of {len(bundles)} bundles …")
    stance_catalog = bootstrap_step.run(bootstrap_bundles)
    save_stance_catalog(stance_catalog, bootstrap_path)
    print(f"  produced {len(stance_catalog.entries)} entries, "
          f"{len(stance_catalog.assignments)} assignments → {bootstrap_path}")
    bootstrapped_now = True
    bootstrap_window_covered = True
else:
    stance_catalog = StanceCatalog(customer.entity_id)
    if not bundles:
        print("Starting from empty catalog (no bundles available)")
    else:
        print("Starting from empty catalog (BOOTSTRAP_IF_MISSING=False)")

print()
print_catalog_overview(stance_catalog)


# ── 5. Streaming setup ────────────────────────────────────────────────────────

state = StreamingState(customer=customer, stance_catalog=stance_catalog)
pipeline = StreamingTagsPipeline(
    state=state,
    triage_step=triage_step,
    stance_tagger=stance_tagger,
    stance_updater=stance_updater,
    claim_tagger=claim_tagger,
    claim_updater=claim_updater,
)
stats = StreamingStats()


# ── 6. Streaming loop — one ArticleBundle at a time ──────────────────────────

if RUN_STREAMING and bundles:
    print_corpus_accounting(
        bundles,
        bootstrapped_now=bootstrapped_now,
        bootstrap_bundle_limit=BOOTSTRAP_BUNDLE_LIMIT,
    )
    # Skip bundles bootstrap already covered (their items are tagged in
    # the catalog already, including null-stance rows for un-clustered
    # triage hints — see bootstrap.py). Set `streaming_start` manually
    # to resume from a higher bundle index after an interruption.
    streaming_start = BOOTSTRAP_BUNDLE_LIMIT if bootstrap_window_covered else 0
    print()
    print(f"Streaming bundles {streaming_start + 1}..{len(bundles)} "
          f"({len(bundles) - streaming_start} of {len(bundles)}) …")
    for i, bundle in enumerate(bundles[streaming_start:], start=streaming_start + 1):
        before_counts = per_entry_counts(state.stance_catalog)
        result = pipeline.process_bundle(bundle)
        stats.absorb(result)
        print_bundle_progress(
            state, bundle, result,
            index=i, total=len(bundles),
            before_counts=before_counts,
            snapshot_top_n=SNAPSHOT_TOP_N,
        )
        if CONSISTENCY_AT_BUNDLE is not None and i == CONSISTENCY_AT_BUNDLE:
            assert False
            run_consistency_pass_at_bundle(state, consistency_step, index=i)
        if CATALOG_SUMMARY_EVERY and i % CATALOG_SUMMARY_EVERY == 0:
            print()
            print(f"=== Catalog snapshot @ bundle {i}/{len(bundles)} ===")
            print_catalogs_summary(state)
            print()


# ── 7. Optional — consistency pass over the run snapshot ─────────────────────

consistency_result = None
if RUN_CONSISTENCY:
    print()
    print("Running consistency pass …")
    consistency_result = consistency_step.run(
        state.stance_catalog, state.items_seen
    )
    if consistency_result.summary:
        print(f"  counters: {consistency_result.summary.counters}")
    print(f"  proposals applied: {len(consistency_result.proposals)}  "
          f"merges: {len(consistency_result.merge_pairs)}  "
          f"retires: {len(consistency_result.retire_ids)}")


# ── 8. Persist + summary ─────────────────────────────────────────────────────

snapshot_path = TAGS_OUTPUT_DIR / customer.slug / f"run_{RUN_TIMESTAMP}.json"
save_snapshot(state.stance_catalog, state.claim_catalogs, snapshot_path)
print()
print(f"Wrote {snapshot_path}")

if consistency_result is not None:
    import json as _json
    consistency_path = TAGS_OUTPUT_DIR / customer.slug / f"consistency_{RUN_TIMESTAMP}.json"
    consistency_path.parent.mkdir(parents=True, exist_ok=True)
    with open(consistency_path, "w", encoding="utf-8") as f:
        _json.dump(consistency_result.to_dict(), f, ensure_ascii=False, indent=2)
    print(f"Wrote {consistency_path}")


print_top_stances_by_type(state.stance_catalog, top_n=5)
print_sample_source_items(state.stance_catalog, state.claim_catalogs, state.items_seen, n=3)
print_top_events(state.stance_catalog, state.claim_catalogs, n_events=5)


# ── Per-bundle helper for IPython re-runs ────────────────────────────────────


def tag_one(bundle_index_or_source_id):
    """Re-tag a single bundle. Useful in IPython after `%run`.

    Examples:
        tag_one(0)                                  # by index
        tag_one('https://www.facebook.com/…')       # by source_id
    """
    if isinstance(bundle_index_or_source_id, int):
        bundle = bundles[bundle_index_or_source_id]
    else:
        bundle = retriever.bundle_for(bundle_index_or_source_id)
        if bundle is None:
            raise KeyError(bundle_index_or_source_id)
    result = pipeline.process_bundle(bundle)
    print_article_snapshot(
        stats, state.stance_catalog, state.claim_catalogs,
        label=f"manual {bundle.root.id}", top_n=SNAPSHOT_TOP_N,
    )
    return result
