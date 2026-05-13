"""Streaming entry point — userdb-backed, file-simulated for now.

Today this iterates the local linked-fixture (the same one
`run_tags.py` uses) and routes every bundle through the same pipeline
class, but every catalog write goes to userdb via the repo classes in
`db.py`. The RabbitMQ swap-in is the only piece that changes: replace
`_simulated_message_stream` with a `pika` consumer that yields
`TagsMessage` instances and acks after the per-bundle commit.

Each message is processed in one psycopg2 transaction: stance and
claim mutations, plus the counter bump on `tags_entity_state`. If the
worker dies mid-bundle, the message stays unacked and is redelivered;
the unique partial indexes on `stance_assignments` keep redelivery
idempotent.

Consistency pass cadence is driven by `tags_entity_state` thresholds
(see `Customer.consistency_pass_due`). Retention (TTL prune + orphan
GC) runs at the start of each pass; source-item text for Stages 2 and
3 is rebuilt via `SourceItemFetcher` (`source_items.py`).
"""

# ────────────────────────────────────────────────────────────────────────
# Reset-state helper for IPython smoke tests (paste-able).
#
# Wipes the userdb tags tables + the on-disk LLM cache so you can rerun
# the simulated stream from scratch. The DB and cache resets are
# separate cells so you can do either independently.
#
#     # ── 1. wipe the userdb tags tables ─────────────────────────────
#     from src.entities.tags.db import connect_userdb
#     conn = connect_userdb()
#     with conn.cursor() as cur:
#         cur.execute(
#             "TRUNCATE "
#             "  public.stance_assignments, "
#             "  public.stance_entries, "
#             "  public.claim_assignments, "
#             "  public.claim_clusters, "
#             "  public.tags_entity_state "
#             "RESTART IDENTITY CASCADE;"
#         )
#     conn.commit()
#     conn.close()
#
#     # ── 2. wipe the LLM cache directories on disk ─────────────────
#     import shutil
#     from pathlib import Path
#     PROJECT_ROOT = Path("/Users/oscarcuellar/ocn/media/kg/kg")
#     for d in (PROJECT_ROOT / "cache").iterdir():
#         if d.name.startswith("tags_"):
#             shutil.rmtree(d, ignore_errors=True)
#             print(f"removed {d}")
#
# After running both, the next `run_simulated_stream(...)` call starts
# from a clean slate.
# ────────────────────────────────────────────────────────────────────────

from __future__ import annotations

import logging
import math
from typing import Iterator, Optional

import psycopg2.extensions

from src.entities.tags.consistency import ConsistencyPassStep
from src.entities.tags.db import (
    ClaimCatalogStoreRepo,
    EntityStateRepo,
    StanceCatalogRepo,
    connect_userdb,
)
from src.entities.tags.models import (
    Customer,
    StreamRunStats,
    TagsMessage,
)
from src.entities.tags.retrieval import ArticleBundleRetriever
from src.entities.tags.runner import (
    LocalRunConfig,
    build_consistency_step,
    build_streaming_pipeline,
    load_customer,
)
from src.entities.tags.source_items import (
    LocalFileSourceItemFetcher,
    SourceItemFetcher,
)
from src.entities.tags.streaming import StreamingState


logger = logging.getLogger(__name__)


# ── Public entry point ────────────────────────────────────────────────


def run_simulated_stream(
    config: LocalRunConfig,
    *,
    org_id: int,
    query_id: Optional[int] = None,
    bundle_limit: Optional[int] = None,
    consistency_every_n_bundles: Optional[int] = None,
    source_item_fetcher: Optional[SourceItemFetcher] = None,
    final_consistency_pass: bool = True,
) -> StreamRunStats:
    """File-simulated, userdb-backed streaming run.

    Args:
        config: same `LocalRunConfig` used by `runner.run_local_stream`.
            `customer_path`, `linked_path`, `events_path`, and the LLM
            model knobs are all read.
        org_id: customer org context (mandatory). Every assignment row
            is tagged with this.
        query_id: optional saved-search id for traceability.
        bundle_limit: stop after this many bundles (for smoke tests).
        consistency_every_n_bundles: if set, run a consistency pass
            every N bundles. If None, defer to
            `customer.consistency_pass_due()` after each bundle.
        source_item_fetcher: where to read raw item text for the
            consistency pass. Defaults to a local-file fetcher over
            `config.linked_path`; pass `ESSourceItemFetcher()` to
            exercise the production path.
        final_consistency_pass: run one last pass after the stream
            drains (default True).

    Returns: `StreamRunStats` with counts.
    """
    if source_item_fetcher is None:
        source_item_fetcher = LocalFileSourceItemFetcher(config.linked_path)

    customer = load_customer(config.customer_path)
    conn = connect_userdb()
    stats = StreamRunStats()

    try:
        stance_repo, claim_store, state_repo = _build_repos(
            conn, customer, org_id,
        )
        # Hydrate the in-memory Customer with the DB-side counters so
        # `consistency_pass_due()` reflects what's persisted, not what
        # the JSON fixture says.
        state_repo.apply_counters_to(customer, org_id)
        conn.commit()

        state = StreamingState(
            customer=customer,
            stance_catalog=stance_repo,
            claim_catalogs=claim_store,
        )
        pipeline = build_streaming_pipeline(customer, config, state)
        consistency_step = build_consistency_step(customer, config)

        message_stream = _simulated_message_stream(
            config, customer, org_id=org_id, query_id=query_id,
        )

        for i, msg in enumerate(message_stream, start=1):
            if bundle_limit is not None and i > bundle_limit:
                break

            _handle_message(
                pipeline, stance_repo, claim_store, state_repo, msg, conn,
            )
            stats.bundles_processed = i
            logger.info("[stream %d] processed %s", i, msg.bundle.root.id)

            if _consistency_pass_due(customer, i, consistency_every_n_bundles):
                _run_consistency_pass(
                    consistency_step, customer, state_repo, stance_repo,
                    source_item_fetcher, org_id, conn, stats,
                )

        if final_consistency_pass and customer.bundles_processed_since_last_pass > 0:
            _run_consistency_pass(
                consistency_step, customer, state_repo, stance_repo,
                source_item_fetcher, org_id, conn, stats,
            )

    finally:
        conn.close()

    return stats


# ── Streaming loop helpers ────────────────────────────────────────────


def _build_repos(
    conn: psycopg2.extensions.connection,
    customer: Customer,
    org_id: int,
) -> tuple[StanceCatalogRepo, ClaimCatalogStoreRepo, EntityStateRepo]:
    stance_repo = StanceCatalogRepo(conn, entity_id=customer.entity_id, org_id=org_id)
    claim_store = ClaimCatalogStoreRepo(conn, entity_id=customer.entity_id, org_id=org_id)
    state_repo = EntityStateRepo(conn)
    state_repo.ensure(customer.entity_id, org_id)
    return stance_repo, claim_store, state_repo


def _handle_message(
    pipeline,
    stance_repo: StanceCatalogRepo,
    claim_store: ClaimCatalogStoreRepo,
    state_repo: EntityStateRepo,
    msg: TagsMessage,
    conn: psycopg2.extensions.connection,
) -> None:
    """Process one message in one DB transaction (commit-or-rollback)."""
    stance_repo.set_bundle_context(msg.bundle, msg.query_id)
    claim_store.set_bundle_context(msg.bundle, msg.query_id)
    try:
        pipeline.process_bundle(msg.bundle)
        state_repo.bump_streaming(msg.entity_id, msg.org_id, n_items=1, n_bundles=1)
        conn.commit()
    except Exception:
        conn.rollback()
        raise


def _consistency_pass_due(
    customer: Customer,
    bundles_processed: int,
    consistency_every_n_bundles: Optional[int],
) -> bool:
    if consistency_every_n_bundles is not None:
        return bundles_processed % consistency_every_n_bundles == 0
    from datetime import datetime, timezone
    return customer.consistency_pass_due(datetime.now(timezone.utc))


def _run_consistency_pass(
    consistency_step: ConsistencyPassStep,
    customer: Customer,
    state_repo: EntityStateRepo,
    stance_repo: StanceCatalogRepo,
    source_item_fetcher: SourceItemFetcher,
    org_id: int,
    conn: psycopg2.extensions.connection,
    stats: StreamRunStats,
) -> None:
    """Run retention → fetch source items → run pass → mark state."""
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


def _simulated_message_stream(
    config: LocalRunConfig,
    customer: Customer,
    *,
    org_id: int,
    query_id: Optional[int],
) -> Iterator[TagsMessage]:
    """Yields one `TagsMessage` per bundle from the local fixture.

    Swap this generator for a `pika`-backed consumer that yields the
    same shape from the RabbitMQ queue. The processing loop above
    doesn't care about the message origin.
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
