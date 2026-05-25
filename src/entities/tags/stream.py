"""Streaming entry point — userdb-backed, file-simulated for now.

This is a script, not a library: paste the whole file (or selected
cells) into IPython and the names live in module scope so you can poke
at `customer`, `stance_repo`, `state_repo`, `messages`, etc. between
bundles.

Phases:
- Phase 1 (bootstrap): one-shot seed of the per-`(entity, org)` stance
  catalog from `BOOTSTRAP_BUNDLE_LIMIT` bundles. Runs only if
  `tags_entity_state.bootstrap_completed_at` is NULL. Writes go
  through `bootstrap_step.run(..., catalog=stance_repo)` — same
  catalog method surface as the streaming loop, so every `add` /
  `assign` lands in userdb.
- Phase 2 (streaming): the main loop below. One bundle = one psycopg2
  transaction (stance + claim mutations + counter bump on
  `tags_entity_state`). Redelivery is idempotent via the unique
  indexes on `stance_assignments` (`ON CONFLICT DO UPDATE`) and
  `claim_assignments` (`ON CONFLICT DO NOTHING`).
- Phase 3 (consistency pass): retention (TTL prune + orphan GC) →
  recent-bundle window → `SourceItemFetcher.fetch_for_assignments` →
  Stages 1/2/3 → `mark_consistency_pass`. Fires when
  `consistency_pass_due()` says so (per-`(entity, org)` thresholds
  in `tags_entity_state`).

The per-message machinery lives in `loop_helpers.py`. To flip from
file-simulated to RabbitMQ, swap `simulated_message_stream` there
for a `pika`-backed generator yielding `TagsMessage`.
"""

from __future__ import annotations

# ────────────────────────────────────────────────────────────────────────
# Reset-state helper for IPython smoke tests (paste-able).
#
# Wipes the userdb tags tables + the on-disk LLM cache so you can rerun
# the simulated stream from scratch. The DB and cache resets are
# separate cells so you can do either independently.
#
#     # ── 1. wipe the userdb tags tables ─────────────────────────────
#     # `RESTART IDENTITY` is intentionally omitted — it requires
#     # ownership of the *_record_id_seq sequences, not just USAGE, and
#     # the `backend` role doesn't own them. The record_ids keep
#     # counting across resets; nothing user-facing references them so
#     # this is fine for smoke tests.
'''
     from src.entities.tags.db import connect_userdb
     _c = connect_userdb()
     with _c.cursor() as cur:
         cur.execute(
             "TRUNCATE "
             "  public.stance_assignments, "
             "  public.stance_entries, "
             "  public.claim_assignments, "
             "  public.claim_clusters, "
             "  public.tags_entity_state "
             "CASCADE;"
         )
     _c.commit()
     _c.close()
'''
#
#     # ── 2. wipe the LLM cache directories on disk ─────────────────
#     import shutil
#     from pathlib import Path
#     for d in (Path("/Users/oscarcuellar/ocn/media/kg/kg/cache")).iterdir():
#         if d.name.startswith("tags_"):
#             shutil.rmtree(d, ignore_errors=True)
#             print(f"removed {d}")
# ────────────────────────────────────────────────────────────────────────

import logging
import sys
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv

# Ensure the project root is on sys.path so `src.*` imports resolve when
# this file is run directly (`ipython src/entities/tags/stream.py`).
_PROJECT_ROOT = Path(
    "/Users/oscarcuellar/ocn/media/kg/kg/src/entities/tags/stream.py"
).resolve().parents[3]
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

load_dotenv(_PROJECT_ROOT / ".env.local")

logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")
logging.getLogger("src.entities.tags").setLevel(logging.INFO)

# Per-phase prompt+response loggers. Set one (or several) to DEBUG to
# dump the full payload for that phase. Uncomment whatever you want to
# debug.
#
# logging.getLogger("tags.prompts").setLevel(logging.DEBUG)             # all six at once
# logging.getLogger("tags.prompts.bootstrap").setLevel(logging.DEBUG)   # BootstrapStep
# logging.getLogger("tags.prompts.triage").setLevel(logging.DEBUG)      # TypeTriageStep
# logging.getLogger("tags.prompts.tagging").setLevel(logging.DEBUG)     # StanceTagger (tag_per_type)
# logging.getLogger("tags.prompts.claim_tag").setLevel(logging.DEBUG)   # ClaimTagger (extraction)
# logging.getLogger("tags.prompts.claim_group").setLevel(logging.DEBUG) # ClaimUpdater (grouping)
# logging.getLogger("tags.prompts.consistency").setLevel(logging.DEBUG) # ConsistencyPassStep


from src.entities.tags.db import connect_userdb
from src.entities.tags.loop_helpers import (
    build_repos,
    consistency_pass_due,
    handle_message,
    run_consistency_pass,
)
from src.entities.tags.models import StreamRunStats, TagsMessage
from src.entities.tags.retrieval import ArticleBundleRetriever
from src.entities.tags.runner import (
    LocalRunConfig,
    build_bootstrap_step,
    build_consistency_step,
    build_streaming_pipeline,
    load_customer,
)
from src.entities.tags.source_items import LocalFileSourceItemFetcher
from src.entities.tags.streaming import StreamingState


# ── Paths ─────────────────────────────────────────────────────────────

CUSTOMER_FIXTURE: Path = _PROJECT_ROOT / "data" / "tags" / "customer_75.json"

# Input fixture. Two supported shapes — same loader (`ArticleBundleRetriever`)
# handles both:
#   • Pre-linked fixture (output of `scripts/build_linked_fixture.py`):
#     docs carry `event_ids`, and a sibling `<stem>__events.json` exists.
#     Claim extraction runs per linked event.
#   • Raw news fixture (output of `PoC/get_data.py`): no `event_ids`, no
#     sibling events file. Bundles yield empty `linked_events`, so the
#     streaming pipeline only runs the stance side (triage + tagging);
#     claim extraction is skipped. Useful for testing the stance loop in
#     isolation.
LINKED_FIXTURE: Path = (
    _PROJECT_ROOT
    / "data"
    / "ayuntamiento_qro"
    / "ayuntamiento_qro_20260522_094734.json"
)
EVENTS_FIXTURE: Path = LINKED_FIXTURE.with_name(f"{LINKED_FIXTURE.stem}__events.json")

TAGS_OUTPUT_DIR: Path = _PROJECT_ROOT / "data" / "tags"


# ── Streaming knobs (edit before re-running) ──────────────────────────

ORG_ID: int = 93
QUERY_ID: Optional[int] = 183

# Phase 1 — bootstrap (only runs when tags_entity_state.bootstrap_completed_at
# is NULL for this (entity, org); set BOOTSTRAP_IF_MISSING=False to skip
# even on a fresh install).
BOOTSTRAP_IF_MISSING: bool = True
BOOTSTRAP_BUNDLE_LIMIT: int = 40

# Phase 2 — streaming.
BUNDLE_LIMIT: Optional[int] = None  # cap remaining bundles after bootstrap; None = all

# Phase 3 — consistency-pass cadence. If set, fires every N bundles
# regardless of the per-customer thresholds. If None, defer to
# `customer.consistency_pass_due()` (which reads the thresholds from
# `tags_entity_state`).
CONSISTENCY_EVERY_N_BUNDLES: Optional[int] = 40
FINAL_CONSISTENCY_PASS: bool = True

# Max age (in days) for any bundle in the consistency-pass window.
# Bundles whose most-recent assignment is older than this are excluded
# even if K hasn't been filled, so the LLM stages only see recent
# material. Set to None to disable the cutoff (rely on K alone).
CONSISTENCY_WINDOW_MAX_AGE_DAYS: Optional[int] = 3

# Backfill mode: when True, the userdb repos stamp every
# `stance_assignments.assigned_at` and `claim_assignments.extracted_at`
# with the bundle item's `created_at` (article `date_created`) rather
# than wall-clock. Lets the 3-day consistency-window cutoff above behave
# correctly when replaying a static corpus all at once — without this,
# every row gets a "just now" timestamp and the age cutoff is a no-op.
# Set False for real live streaming.
SIMULATE_ASSIGNED_AT_FROM_DOCUMENT: bool = True

INCLUDE_COMMENTS_IN_CLAIMS: bool = False


# ── Build the LocalRunConfig (env-var-driven model defaults) ──────────

config = LocalRunConfig(
    customer_path=CUSTOMER_FIXTURE,
    linked_path=LINKED_FIXTURE,
    events_path=EVENTS_FIXTURE,
    output_dir=TAGS_OUTPUT_DIR,
    include_comments=INCLUDE_COMMENTS_IN_CLAIMS,
)
print(f"models: triage={config.triage_model}")
print(f"        bootstrap={config.bootstrap_model}")
print(f"        tagger={config.tagger_model}")
print(f"        claim_tagger={config.claim_tagger_model}")
print(f"        claim_updater={config.claim_updater_model}")
print(f"        consistency={config.consistency_model}")


# ── Setup ─────────────────────────────────────────────────────────────

customer = load_customer(config.customer_path)
source_item_fetcher = LocalFileSourceItemFetcher(config.linked_path)
conn = connect_userdb()
stats = StreamRunStats()

stance_repo, claim_store, state_repo = build_repos(
    conn, customer, ORG_ID,
    simulate_assigned_at_from_document=SIMULATE_ASSIGNED_AT_FROM_DOCUMENT,
)

# Hydrate the in-memory Customer with the DB-side counters so
# `consistency_pass_due()` reflects what's persisted, not what the JSON
# fixture says.
state_repo.apply_counters_to(customer, ORG_ID)
conn.commit()

state = StreamingState(
    customer=customer,
    stance_catalog=stance_repo,
    claim_catalogs=claim_store,
)
pipeline = build_streaming_pipeline(customer, config, state)
bootstrap_step = build_bootstrap_step(customer, config)
consistency_step = build_consistency_step(customer, config)

retriever = ArticleBundleRetriever(
    config.linked_path, config.events_path, customer=customer,
)
all_bundles = list(retriever.iter_bundles())

print(
    f"customer entity_id={customer.entity_id}  org_id={ORG_ID}  "
    f"bundles_total_so_far={customer.bundles_processed_total}  "
    f"since_last_pass={customer.bundles_processed_since_last_pass}  "
    f"corpus_size={len(all_bundles)}"
)


# ── Phase 1 — bootstrap (one-shot per (entity, org)) ──────────────────

_state_row = state_repo.load(customer.entity_id, ORG_ID) or {}
already_bootstrapped = _state_row.get("bootstrap_completed_at") is not None
bootstrapped_now = False

if already_bootstrapped:
    print(
        f"[bootstrap] already done at {_state_row['bootstrap_completed_at']} — "
        f"skipping Phase 1"
    )
elif BOOTSTRAP_IF_MISSING and all_bundles:
    _bootstrap_corpus = all_bundles[:BOOTSTRAP_BUNDLE_LIMIT]
    print(
        f"[bootstrap] running Phase 1 on {len(_bootstrap_corpus)} of "
        f"{len(all_bundles)} bundles …"
    )
    bootstrap_step.run(_bootstrap_corpus, catalog=stance_repo, query_id=QUERY_ID)
    state_repo.mark_bootstrap_complete(
        customer.entity_id, ORG_ID,
        stream_now=stance_repo.effective_now(),
    )
    conn.commit()
    bootstrapped_now = True
    print(f"[bootstrap] committed — userdb now has the seed catalog")
else:
    print(
        "[bootstrap] starting from empty catalog "
        "(BOOTSTRAP_IF_MISSING=False or no bundles)"
    )


# ── Phase 2 — streaming setup ─────────────────────────────────────────
#
# If bootstrap just ran on bundles[:BOOTSTRAP_BUNDLE_LIMIT], skip that
# window in streaming — those items already have assignments. On reruns
# where bootstrap was already done, we don't know what's been streamed
# since (the DB has it but we'd duplicate-LLM the bundles); set
# BUNDLE_LIMIT to a small number while exploring.

_streaming_start = BOOTSTRAP_BUNDLE_LIMIT if bootstrapped_now else 0
_streaming_bundles = all_bundles[_streaming_start:]
if BUNDLE_LIMIT is not None:
    _streaming_bundles = _streaming_bundles[:BUNDLE_LIMIT]

# Generator of `TagsMessage`. Stays paused between bundles — IPython
# can call `msg = next(messages)` to step one at a time, or the loop
# below to drain the rest.
messages = (
    TagsMessage(
        bundle=b,
        entity_id=customer.entity_id,
        org_id=ORG_ID,
        query_id=QUERY_ID,
    )
    for b in _streaming_bundles
)
print(f"[stream] {len(_streaming_bundles)} bundles to stream")


# ── Main loop ─────────────────────────────────────────────────────────
#
# For step-by-step debugging in IPython, skip this loop and call
# `next(messages)` + `handle_message(...)` yourself between
# inspections. The loop below drains the rest of the stream when you
# want to fast-forward.
#
#     msg = next(messages)
#     handle_message(pipeline, stance_repo, claim_store, state_repo, msg, conn)
#     # inspect: state, customer, stats, or query userdb directly
#     # repeat ...

for i, msg in enumerate(messages, start=5):
    handle_message(pipeline, stance_repo, claim_store, state_repo, msg, conn)
    stats.bundles_processed = i
    print(f"[stream {i}/{len(_streaming_bundles)}] processed {msg.bundle.root.id}")

    if consistency_pass_due(customer, i, CONSISTENCY_EVERY_N_BUNDLES, stance_repo):
        run_consistency_pass(
            consistency_step, customer, state_repo, stance_repo,
            source_item_fetcher, ORG_ID, conn, stats,
            window_max_age_days=CONSISTENCY_WINDOW_MAX_AGE_DAYS,
        )


# ── Phase 3 — final consistency pass ──────────────────────────────────

if FINAL_CONSISTENCY_PASS and customer.bundles_processed_since_last_pass > 0:
    run_consistency_pass(
        consistency_step, customer, state_repo, stance_repo,
        source_item_fetcher, ORG_ID, conn, stats,
    )

print(
    f"done. bundles={stats.bundles_processed} "
    f"consistency_passes={stats.consistency_passes}"
)

# Close the userdb connection by hand when you're finished poking
# around (uncomment when shutting down the IPython session):
#
#     conn.close()
