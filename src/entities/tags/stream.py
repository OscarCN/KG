"""Streaming entry point — userdb-backed, file-simulated for now.

Today this iterates the local linked-fixture (the same one
`run_tags.py` uses) and routes every bundle through the same pipeline
class, but every catalog write goes to userdb via the repo classes in
`db.py`. The RabbitMQ swap-in is the only piece that changes: replace
`loop_helpers.simulated_message_stream` with a `pika` consumer that
yields `TagsMessage` instances and acks after the per-bundle commit.

Each message is processed in one psycopg2 transaction: stance and
claim mutations, plus the counter bump on `tags_entity_state`. If the
worker dies mid-bundle, the message stays unacked and is redelivered.
Redelivery is idempotent: stance writes hit a single unique index on
`(source_item_id, entity_id, org_id, stance_type)` with
`ON CONFLICT DO UPDATE` (latest tagging wins, one row per item/type),
and claim writes hit a unique index on
`(source_item_id, entity_id, org_id, event_id, cluster_id,
verbatim_hash)` with `ON CONFLICT DO NOTHING`.

Consistency pass cadence is driven by `tags_entity_state` thresholds
(see `Customer.consistency_pass_due`). Retention (TTL prune + orphan
GC) runs at the start of each pass; source-item text for Stages 2 and
3 is rebuilt via `SourceItemFetcher` (`source_items.py`).

This file is intentionally lean: `run_simulated_stream` is the main
loop, paste-and-call from IPython. The per-message machinery lives in
`loop_helpers.py`.
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
from typing import Optional

from src.entities.tags.db import connect_userdb
from src.entities.tags.loop_helpers import (
    build_repos,
    consistency_pass_due,
    handle_message,
    run_consistency_pass,
    simulated_message_stream,
)
from src.entities.tags.models import StreamRunStats
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
    """File-simulated, userdb-backed streaming run (IPython entry point).

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
        stance_repo, claim_store, state_repo = build_repos(conn, customer, org_id)
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

        messages = simulated_message_stream(
            config, customer, org_id=org_id, query_id=query_id,
        )

        for i, msg in enumerate(messages, start=1):
            if bundle_limit is not None and i > bundle_limit:
                break

            handle_message(
                pipeline, stance_repo, claim_store, state_repo, msg, conn,
            )
            stats.bundles_processed = i
            logger.info("[stream %d] processed %s", i, msg.bundle.root.id)

            if consistency_pass_due(customer, i, consistency_every_n_bundles):
                run_consistency_pass(
                    consistency_step, customer, state_repo, stance_repo,
                    source_item_fetcher, org_id, conn, stats,
                )

        if final_consistency_pass and customer.bundles_processed_since_last_pass > 0:
            run_consistency_pass(
                consistency_step, customer, state_repo, stance_repo,
                source_item_fetcher, org_id, conn, stats,
            )

    finally:
        conn.close()

    return stats
