"""Message-loop helpers for `stream.py`.

This file holds the production-shaped per-message plumbing that the
streaming script in `stream.py` calls into. Split out so that
`stream.py` can stay a thin, IPython-pasteable orchestration script
focused on setup + main loop. Swapping `simulated_message_stream`
for a `pika` consumer is the one change that flips this from
file-simulated to RabbitMQ-driven.

Not to be confused with `test_helpers.py`, which holds the
printout/debug helpers used by the in-memory IPython driver
(`run_tags.py`).
"""

from __future__ import annotations

import logging
import math
from datetime import datetime, timezone
from typing import Iterator, Optional

import psycopg2.extensions

from src.entities.tags.consistency import ConsistencyPassStep
from src.entities.tags.db import (
    ClaimCatalogStoreRepo,
    EntityStateRepo,
    StanceCatalogRepo,
)
from src.entities.tags.models import (
    Customer,
    StreamRunStats,
    TagsMessage,
)
from src.entities.tags.retrieval import ArticleBundleRetriever
from src.entities.tags.runner import LocalRunConfig
from src.entities.tags.source_items import SourceItemFetcher


logger = logging.getLogger(__name__)


# ── Repo + state setup ────────────────────────────────────────────────


def build_repos(
    conn: psycopg2.extensions.connection,
    customer: Customer,
    org_id: int,
) -> tuple[StanceCatalogRepo, ClaimCatalogStoreRepo, EntityStateRepo]:
    """Build the three userdb-backed repos scoped to `(entity_id, org_id)`.

    `state_repo.ensure(...)` inserts a default `tags_entity_state` row
    if one doesn't yet exist — safe to call on every run.
    """
    stance_repo = StanceCatalogRepo(conn, entity_id=customer.entity_id, org_id=org_id)
    claim_store = ClaimCatalogStoreRepo(conn, entity_id=customer.entity_id, org_id=org_id)
    state_repo = EntityStateRepo(conn)
    state_repo.ensure(customer.entity_id, org_id)
    return stance_repo, claim_store, state_repo


# ── Per-message handler ───────────────────────────────────────────────


def handle_message(
    pipeline,
    stance_repo: StanceCatalogRepo,
    claim_store: ClaimCatalogStoreRepo,
    state_repo: EntityStateRepo,
    msg: TagsMessage,
    conn: psycopg2.extensions.connection,
) -> None:
    """Process one message in one DB transaction (commit-or-rollback).

    Sets the per-bundle enrichment context on both catalog repos so
    `assign(...)` auto-fills `parent_source_id` / `news_type` /
    `query_id` from the bundle's items, runs `process_bundle`, bumps
    the per-`(entity, org)` counters, then commits. On exception
    rolls back so a redelivered RabbitMQ message can replay cleanly.
    """
    stance_repo.set_bundle_context(msg.bundle, msg.query_id)
    claim_store.set_bundle_context(msg.bundle, msg.query_id)
    try:
        pipeline.process_bundle(msg.bundle)
        state_repo.bump_streaming(msg.entity_id, msg.org_id, n_bundles=1)
        conn.commit()
    except Exception:
        conn.rollback()
        raise


# ── Consistency-pass dispatch ─────────────────────────────────────────


def consistency_pass_due(
    customer: Customer,
    bundles_processed: int,
    consistency_every_n_bundles: Optional[int],
) -> bool:
    """Decide whether the consistency pass should run after this bundle.

    If `consistency_every_n_bundles` is set, fires deterministically
    every N bundles. Otherwise defers to `customer.consistency_pass_due`
    which checks `bundles_processed_since_last_pass` and
    `last_consistency_pass_at` against the per-customer thresholds.
    """
    if consistency_every_n_bundles is not None:
        return bundles_processed % consistency_every_n_bundles == 0
    return customer.consistency_pass_due(datetime.now(timezone.utc))


def run_consistency_pass(
    consistency_step: ConsistencyPassStep,
    customer: Customer,
    state_repo: EntityStateRepo,
    stance_repo: StanceCatalogRepo,
    source_item_fetcher: SourceItemFetcher,
    org_id: int,
    conn: psycopg2.extensions.connection,
    stats: StreamRunStats,
) -> None:
    """Retention → fetch source items → run pass → mark state.

    Whole pass lives in one TX: a failure rolls retention back and
    leaves the counters untouched, so the next pass re-attempts.
    """
    try:
        ttl = state_repo.get_ttl_days(customer.entity_id, org_id)
        expired = stance_repo.expire_old_assignments(ttl)
        orphans = stance_repo.gc_orphan_entries()
        logger.info(
            "consistency: retention expired=%d orphans=%d (ttl=%dd)",
            expired, orphans, ttl,
        )

        bundles_since = max(0, customer.bundles_processed_since_last_pass)
        n_bundles = math.ceil(bundles_since * 1.25)
        recent = stance_repo.recent_bundle_assignments(
            n_bundles=n_bundles, kinds=("article", "user_post"),
        )
        items_seen = source_item_fetcher.fetch_for_assignments(recent)
        logger.info(
            "consistency: window=%d bundles, %d assignments, %d items fetched",
            n_bundles, len(recent), len(items_seen),
        )

        result = consistency_step.run(stance_repo, items_seen)
        state_repo.mark_consistency_pass(customer.entity_id, org_id)
        conn.commit()

        stats.consistency_passes += 1
        if result.summary:
            stats.per_pass_summaries.append(dict(result.summary.counters))
            logger.info("consistency pass complete: %s", result.summary.counters)
    except Exception:
        conn.rollback()
        raise


# ── Ingestion sources ─────────────────────────────────────────────────


def simulated_message_stream(
    config: LocalRunConfig,
    customer: Customer,
    *,
    org_id: int,
    query_id: Optional[int],
) -> Iterator[TagsMessage]:
    """Yields one `TagsMessage` per bundle from the local fixture.

    Swap this generator for a `pika`-backed consumer that yields the
    same shape from the RabbitMQ queue. The processing loop in
    `stream.py` doesn't care about the message origin.
    """
    retriever = ArticleBundleRetriever(
        config.linked_path, config.events_path, customer=customer,
    )
    for bundle in retriever.iter_bundles():
        yield TagsMessage(
            bundle=bundle,
            entity_id=customer.entity_id,
            org_id=org_id,
            query_id=query_id,
        )
