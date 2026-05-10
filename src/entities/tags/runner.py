"""Local runner: wires the config + LLMs + steps for the CLI entrypoints.

Reads paths and model overrides from a `LocalRunConfig`, builds cached LLMs
per phase, and delegates to the step classes.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from src.entities.tags.bootstrap import BootstrapStep
from src.entities.tags.catalogs import ClaimCatalogStore, StanceCatalog
from src.entities.tags.consistency import ConsistencyPassStep
from src.entities.tags.llm import (
    OpenRouterJsonLlm,
    CachedJsonLlm,
    cache_dir_for,
)
from src.entities.tags.models import (
    ArticleProcessResult,
    ConsistencyPassResult,
    Customer,
)
from src.entities.tags.persistence import (
    load_snapshot,
    load_stance_catalog,
    save_snapshot,
    save_stance_catalog,
)
from src.entities.tags.retrieval import ArticleBundleRetriever
from src.entities.tags.stats import (
    StreamingStats,
    print_article_snapshot,
    print_event_created_snapshot,
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


logger = logging.getLogger(__name__)


# ── Default models per phase (env-var overridable) ─────────────────────


def _env(name: str, default: str) -> str:
    return os.environ.get(name) or default


@dataclass
class LocalRunConfig:
    """Paths and knobs for a manual run."""

    customer_path: Path
    linked_path: Path
    events_path: Path
    output_dir: Path
    catalog_path: Optional[Path] = None  # bootstrap output, used by streaming

    include_comments: bool = False
    snapshot_top_n: int = 10
    sample_size: int = 300
    bootstrap_corpus_limit: Optional[int] = None

    triage_model: str = field(
        default_factory=lambda: _env(
            "OPENROUTER_TAGS_TRIAGE_MODEL", "google/gemini-2.5-flash-lite"
        )
    )
    bootstrap_model: str = field(
        default_factory=lambda: _env(
            "OPENROUTER_TAGS_BOOTSTRAP_MODEL", "openai/gpt-4o"
        )
    )
    tagger_model: str = field(
        default_factory=lambda: _env(
            "OPENROUTER_TAGS_TAGGER_MODEL", "google/gemini-2.5-flash-lite"
        )
    )
    claim_tagger_model: str = field(
        default_factory=lambda: _env(
            "OPENROUTER_TAGS_CLAIM_TAGGER_MODEL", "google/gemini-2.5-flash-lite"
        )
    )
    claim_updater_model: str = field(
        default_factory=lambda: _env(
            "OPENROUTER_TAGS_CLAIM_UPDATER_MODEL", "google/gemini-2.5-flash-lite"
        )
    )
    consistency_model: str = field(
        default_factory=lambda: _env(
            "OPENROUTER_TAGS_CONSISTENCY_MODEL", "openai/gpt-4o"
        )
    )


# ── Builders ────────────────────────────────────────────────────────────


def load_customer(path: Path) -> Customer:
    with open(path, encoding="utf-8") as f:
        payload = json.load(f)
    return Customer.from_dict(payload)


def _cached_llm(phase: str, customer_id: int, model: str):
    inner = OpenRouterJsonLlm(model=model)
    return CachedJsonLlm(
        inner,
        cache_dir=cache_dir_for(phase, customer_id),
        model=model,
        extra={"phase": phase},
    )


def build_triage_step(customer: Customer, config: LocalRunConfig) -> TypeTriageStep:
    llm = _cached_llm("triage", customer.entity_id, config.triage_model)
    return TypeTriageStep(customer, llm)


def build_streaming_pipeline(
    customer: Customer,
    config: LocalRunConfig,
    state: StreamingState,
) -> StreamingTagsPipeline:
    return StreamingTagsPipeline(
        state=state,
        triage_step=build_triage_step(customer, config),
        stance_tagger=StanceTagger(
            customer, _cached_llm("tagging", customer.entity_id, config.tagger_model)
        ),
        stance_updater=StanceUpdater(),
        claim_tagger=ClaimTagger(
            customer,
            _cached_llm("claim_tag", customer.entity_id, config.claim_tagger_model),
            include_comments=config.include_comments,
        ),
        claim_updater=ClaimUpdater(
            customer,
            _cached_llm("claim_group", customer.entity_id, config.claim_updater_model),
        ),
    )


def build_bootstrap_step(customer: Customer, config: LocalRunConfig) -> BootstrapStep:
    llm = _cached_llm("bootstrap", customer.entity_id, config.bootstrap_model)
    return BootstrapStep(customer, build_triage_step(customer, config), llm)


def build_consistency_step(customer: Customer, config: LocalRunConfig) -> ConsistencyPassStep:
    llm = _cached_llm("consistency", customer.entity_id, config.consistency_model)
    return ConsistencyPassStep(customer, llm, sample_size=config.sample_size)


# ── Top-level orchestration ────────────────────────────────────────────


def run_local_bootstrap(config: LocalRunConfig) -> tuple[Customer, StanceCatalog]:
    customer = load_customer(config.customer_path)
    retriever = ArticleBundleRetriever(
        config.linked_path, config.events_path, customer=customer
    )
    bootstrap = build_bootstrap_step(customer, config)
    bundles = list(retriever.iter_bundles())
    if config.bootstrap_corpus_limit:
        bundles = bundles[: config.bootstrap_corpus_limit]
    print(f"[bootstrap] customer={customer.entity_id} corpus_size={len(bundles)}")
    catalog = bootstrap.run(bundles)

    out_path = config.catalog_path or (
        config.output_dir / customer.slug / "bootstrap.json"
    )
    save_stance_catalog(catalog, out_path)
    print(f"[bootstrap] wrote {out_path}  entries={len(catalog.entries)}")
    print_top_stances_by_type(catalog, top_n=5)
    return customer, catalog


def run_local_stream(config: LocalRunConfig) -> tuple[Customer, StreamingState, StreamingStats]:
    customer = load_customer(config.customer_path)
    retriever = ArticleBundleRetriever(
        config.linked_path, config.events_path, customer=customer
    )

    # Load bootstrap catalog if provided; else start empty.
    if config.catalog_path and config.catalog_path.exists():
        stance_catalog = load_stance_catalog(config.catalog_path)
        print(f"[run] loaded catalog from {config.catalog_path} "
              f"({len(stance_catalog.entries)} entries)")
    else:
        stance_catalog = StanceCatalog(customer.entity_id)
        print("[run] starting from empty catalog")

    state = StreamingState(customer=customer, stance_catalog=stance_catalog)
    pipeline = build_streaming_pipeline(customer, config, state)
    stats = StreamingStats()

    bundles = list(retriever.iter_bundles())
    print(f"[run] streaming {len(bundles)} bundles …")
    for i, bundle in enumerate(bundles, start=1):
        result = pipeline.process_bundle(bundle)
        stats.absorb(result)
        print(f"[{i}/{len(bundles)}] {bundle.root.id}")
        print_article_snapshot(
            stats,
            state.stance_catalog,
            state.claim_catalogs,
            label=f"bundle {i}",
            top_n=config.snapshot_top_n,
        )
        for etr in result.event_tag_results:
            if etr.event_id == "__bundle__":
                continue
            print_event_created_snapshot(
                state.stance_catalog,
                state.claim_catalogs,
                customer.entity_id,
                etr.event_id,
                top_n=config.snapshot_top_n,
            )

    # Final snapshot.
    from datetime import datetime
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    out_path = config.output_dir / customer.slug / f"run_{ts}.json"
    save_snapshot(state.stance_catalog, state.claim_catalogs, out_path)
    print()
    print(f"[run] wrote {out_path}")
    print_top_stances_by_type(state.stance_catalog, top_n=5)
    print_sample_source_items(state.stance_catalog, state.claim_catalogs, state.items_seen, n=3)
    print_top_events(state.stance_catalog, state.claim_catalogs, n_events=5)
    return customer, state, stats


def run_local_consistency(
    config: LocalRunConfig,
) -> tuple[Customer, StanceCatalog, ClaimCatalogStore, ConsistencyPassResult]:
    customer = load_customer(config.customer_path)
    if not config.catalog_path:
        raise ValueError("consistency: --catalog is required")
    stance_catalog, claim_catalogs = load_snapshot(config.catalog_path)
    print(f"[consistency] loaded catalog from {config.catalog_path}  "
          f"entries={len(stance_catalog.entries)}  "
          f"assignments={len(stance_catalog.assignments)}")

    items_seen: dict = {}  # consistency pass uses items if available; empty is OK

    step = build_consistency_step(customer, config)
    result = step.run(stance_catalog, items_seen, claim_catalogs)

    from datetime import datetime
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    out_path = config.output_dir / customer.slug / f"consistency_{ts}.json"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(result.to_dict(), f, ensure_ascii=False, indent=2)
    snapshot_path = config.output_dir / customer.slug / f"run_after_consistency_{ts}.json"
    save_snapshot(stance_catalog, claim_catalogs, snapshot_path)
    print(f"[consistency] wrote {out_path}")
    print(f"[consistency] wrote {snapshot_path}")
    if result.summary:
        print(f"[consistency] counters: {result.summary.counters}")
    return customer, stance_catalog, claim_catalogs, result
