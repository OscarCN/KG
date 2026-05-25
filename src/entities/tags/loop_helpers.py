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
    *,
    simulate_assigned_at_from_document: bool = False,
) -> tuple[StanceCatalogRepo, ClaimCatalogStoreRepo, EntityStateRepo]:
    """Build the three userdb-backed repos scoped to `(entity_id, org_id)`.

    `state_repo.ensure(...)` inserts a default `tags_entity_state` row
    if one doesn't yet exist — safe to call on every run.

    Also sweeps the catalogue once at startup: `retire_stale_entries(ttl)`
    soft-retires any entry whose most-recent `stance_assignments` row is
    older than `assignment_ttl_days`. Without this, a run that resumes
    after a long gap (or after a crash before the previous pass fired)
    would tag against entries whose evidence is stale — the consistency
    pass would only catch them on its next scheduled run. Counters on
    `tags_entity_state` are intentionally not touched here (this is
    retention, not a full consistency pass).

    `simulate_assigned_at_from_document` is the backfill-testing knob:
    when True, every `stance_assignments.assigned_at` and
    `claim_assignments.extracted_at` row written by these repos uses
    the bundle item's `created_at` (article `date_created`) instead of
    wall-clock now. Comments inherit their parent post's `created_at`.
    Off by default — live streaming wants wall-clock.
    """
    stance_repo = StanceCatalogRepo(
        conn, entity_id=customer.entity_id, org_id=org_id,
        simulate_assigned_at_from_document=simulate_assigned_at_from_document,
    )
    claim_store = ClaimCatalogStoreRepo(
        conn, entity_id=customer.entity_id, org_id=org_id,
        simulate_assigned_at_from_document=simulate_assigned_at_from_document,
    )
    state_repo = EntityStateRepo(conn)
    state_repo.ensure(customer.entity_id, org_id)

    # Seed the stream clock from the latest `assigned_at` already on
    # disk so the startup retire runs against the stream's last-known
    # position rather than today's date. Without this anchor, a
    # backfill re-run that resumes weeks after the previous one would
    # use wall-clock and immediately retire everything inserted in the
    # prior session.
    startup_now = _latest_assigned_at(conn, customer.entity_id, org_id)
    if startup_now is not None:
        stance_repo.set_stream_now(startup_now)
        logger.info("startup stream clock anchored at %s", startup_now.isoformat())

    ttl = state_repo.get_ttl_days(customer.entity_id, org_id)
    retired = stance_repo.retire_stale_entries(ttl)
    logger.info("startup retention: retired=%d (ttl=%dd)", retired, ttl)

    return stance_repo, claim_store, state_repo


def _latest_assigned_at(
    conn: psycopg2.extensions.connection, entity_id: int, org_id: int,
) -> Optional[datetime]:
    """Latest `assigned_at` on disk for this `(entity, org)` scope.

    Used as the startup stream clock so retention at startup operates
    at the document timeline the previous run left off at, not today.
    Returns None when no assignments exist (first-ever run) — the repo
    then falls back to wall-clock until the first bundle arrives.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT MAX(assigned_at)
              FROM stance_assignments
             WHERE entity_id = %s AND org_id = %s
            """,
            (entity_id, org_id),
        )
        row = cur.fetchone()
        if not row or row[0] is None:
            return None
        ts = row[0]
        if isinstance(ts, datetime):
            return ts if ts.tzinfo else ts.replace(tzinfo=timezone.utc)
        return None


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
    stance_repo: Optional[StanceCatalogRepo] = None,
) -> bool:
    """Decide whether the consistency pass should run after this bundle.

    If `consistency_every_n_bundles` is set, fires deterministically
    every N bundles. Otherwise defers to `customer.consistency_pass_due`
    which checks `bundles_processed_since_last_pass` and
    `last_consistency_pass_at` against the per-customer thresholds —
    that time check uses the repo's stream clock (`effective_now()`)
    when supplied so a backfill replay decides "is the next pass due"
    against the document timeline, not wall-clock. Pass `stance_repo`
    from the caller; fall back to wall-clock only when no repo is
    handy (legacy callers, isolated tests).
    """
    if consistency_every_n_bundles is not None:
        return bundles_processed % consistency_every_n_bundles == 0
    now = stance_repo.effective_now() if stance_repo is not None else datetime.now(timezone.utc)
    return customer.consistency_pass_due(now)


def run_consistency_pass(
    consistency_step: ConsistencyPassStep,
    customer: Customer,
    state_repo: EntityStateRepo,
    stance_repo: StanceCatalogRepo,
    source_item_fetcher: SourceItemFetcher,
    org_id: int,
    conn: psycopg2.extensions.connection,
    stats: StreamRunStats,
    *,
    window_max_age_days: Optional[int] = 3,
) -> None:
    """Retention → fetch source items → run pass → mark state.

    Whole pass lives in one TX: a failure rolls retention back and
    leaves the counters untouched, so the next pass re-attempts.

    `window_max_age_days` caps the consistency window: bundles whose
    most-recent assignment is older than that cutoff are excluded even
    if K hasn't been filled. Default 3d — tighter than the assignment
    TTL so the LLM operates on what's actually current.

    Retention is **soft-retire only**: entries with no assignment
    newer than `assignment_ttl_days` are stamped `retired_at = now()`
    so they drop out of the active catalogue but stay in the database
    along with their full assignment history.
    """
    try:
        ttl = state_repo.get_ttl_days(customer.entity_id, org_id)
        retired = stance_repo.retire_stale_entries(ttl)
        logger.info(
            "consistency: retention retired=%d (ttl=%dd)",
            retired, ttl,
        )

        bundles_since = max(0, customer.bundles_processed_since_last_pass)
        n_bundles = math.ceil(bundles_since * 1.25)
        recent = stance_repo.recent_bundle_assignments(
            n_bundles=n_bundles,
            kinds=("article", "user_post"),
            max_age_days=window_max_age_days,
        )
        items_seen = source_item_fetcher.fetch_for_assignments(recent)
        logger.info(
            "consistency: window K=%d max_age=%s days, %d assignments, %d items fetched",
            n_bundles, window_max_age_days, len(recent), len(items_seen),
        )

        result = consistency_step.run(stance_repo, items_seen)
        state_repo.mark_consistency_pass(
            customer.entity_id, org_id,
            stream_now=stance_repo.effective_now(),
        )
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
