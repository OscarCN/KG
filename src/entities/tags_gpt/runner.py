"""Convenience runner for the decoupled streaming pipeline."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Optional

from src.entities.linking_gpt import TagsGptLinkingAdapter
from src.entities.tags_gpt.bootstrap import StanceBootstrapStep
from src.entities.tags_gpt.catalogs import ClaimCatalogStore, EventStore
from src.entities.tags_gpt.consistency import ConsistencyPassStep
from src.entities.tags_gpt.extraction import group_by_source, load_extracted_records, sort_batches_by_publication
from src.entities.tags_gpt.llm import JsonLlm, default_cached_llm
from src.entities.tags_gpt.persistence import load_content_graph, save_snapshot
from src.entities.tags_gpt.retrieval import ContentRetriever, EsNewsRetriever, LocalJsonRetriever
from src.entities.tags_gpt.streaming import StreamingState, StreamingTagsPipeline
from src.entities.tags_gpt.tagging import ClaimTagger, ClaimUpdater, StanceTagger, StanceUpdater, TypeTriageStep


@dataclass
class LocalRunConfig:
    extracted_records_path: Path
    customer_fixture_path: Path
    news_json_path: Optional[Path] = None
    snapshot_path: Optional[Path] = None
    bootstrap_corpus_limit: int = 80
    geocode: bool = True
    tagging_strategy: Literal["single_pass", "two_pass"] = "two_pass"
    triage_comment_batch_size: int = 12
    run_consistency_pass: bool = False
    consistency_sample_size: int = 300


@dataclass
class LocalRunResult:
    state: StreamingState
    article_results: list
    linker: TagsGptLinkingAdapter
    consistency_result: object = None


def run_local_stream(
    config: LocalRunConfig,
    *,
    llm: Optional[JsonLlm] = None,
    retriever: Optional[ContentRetriever] = None,
) -> LocalRunResult:
    """Run the decoupled pipeline over local extracted records.

    All heavyweight dependencies are injectable. In tests, pass a fake
    retriever and `ScriptedJsonLlm`; in exploratory runs, leave them as
    defaults and provide a local news JSON file or ES credentials.
    """
    llm = llm or default_cached_llm()
    graph = load_content_graph(config.customer_fixture_path)
    customer = graph.customer

    records = load_extracted_records(config.extracted_records_path)
    batches = sort_batches_by_publication(group_by_source(records))
    source_ids = [batch.source_id for batch in batches]

    retriever = retriever or _default_retriever(config)
    corpus = retriever.get_customer_corpus(source_ids, limit=config.bootstrap_corpus_limit)
    stance_catalog = StanceBootstrapStep(llm).bootstrap(customer, corpus)

    event_store = EventStore()
    state = StreamingState(
        event_store=event_store,
        stance_catalog=stance_catalog,
        claim_catalogs=ClaimCatalogStore(),
        tagging_strategy=config.tagging_strategy,
    )
    linker = TagsGptLinkingAdapter(event_store=event_store, geocode=config.geocode)
    stance_updater = StanceUpdater(customer, llm)
    pipeline = StreamingTagsPipeline(
        state=state,
        retriever=retriever,
        linker=linker,
        stance_tagger=StanceTagger(customer, llm),
        stance_updater=stance_updater,
        claim_tagger=ClaimTagger(customer, llm),
        claim_updater=ClaimUpdater(customer, llm),
        type_triage_step=TypeTriageStep(customer, llm),
        triage_comment_batch_size=config.triage_comment_batch_size,
    )

    article_results = [pipeline.process_batch(batch) for batch in batches]
    consistency_result = None
    if config.run_consistency_pass:
        consistency_result = ConsistencyPassStep(
            customer,
            llm,
            stance_updater,
            sample_size=config.consistency_sample_size,
        ).run(
            state.stance_catalog,
            items_seen=state.items_seen,
            claim_catalogs=state.claim_catalogs,
        )
    if config.snapshot_path:
        save_snapshot(
            config.snapshot_path,
            event_store=state.event_store,
            stance_catalog=state.stance_catalog,
            claim_catalogs=state.claim_catalogs,
        )
    return LocalRunResult(
        state=state,
        article_results=article_results,
        linker=linker,
        consistency_result=consistency_result,
    )


def _default_retriever(config: LocalRunConfig) -> ContentRetriever:
    if config.news_json_path:
        return LocalJsonRetriever(config.news_json_path)
    cache_dir = Path(__file__).resolve().parents[3] / "cache" / "tags_gpt_es_articles"
    return EsNewsRetriever(cache_dir=cache_dir)
