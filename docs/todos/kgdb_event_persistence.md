# TODO — Persist linked events into kgdb (Step Zero → streaming write/merge)

**Status:** open — design ready; **sequenced after** the retrieval/linking improvements below
**Area:** new `src/entities/linking/persistence.py`, `scripts/persist_linked.py`, `scripts/gen_kg_catalog_seed.py`; kgdb migrations in `media-backend-paid/db/kg_db/`
**Related:** [`canonical_reconciliation.md`](canonical_reconciliation.md), [`retrieval_name_soft_type.md`](retrieval_name_soft_type.md), [`location_level_list_extraction.md`](location_level_list_extraction.md), [`../linking.md`](../linking.md), [`../../../../media-backend-paid/docs/DATABASE_POSTGRES.md`](../../../../media-backend-paid/docs/DATABASE_POSTGRES.md)

## Sequencing

Do **not** start this until the linking quality work lands, since persisting now would write
the known-fragmented output (the Zona Fest split) into kgdb:

1. [Multi-venue / multi-street locations](location_level_list_extraction.md) — `locations:
   List[Location]`, so multi-place events geocode to several level-6/7 rows.
2. [Soft name/type retrieval + multi-match](retrieval_name_soft_type.md) — hard date+geo, soft
   name/type, name-similarity retrieval; produces the multi-match candidate sets.
3. [Canonical↔canonical reconciliation](canonical_reconciliation.md) — the in-DB merge this
   write path eventually needs.

**Then** Step Zero below.

## End-state

A **RabbitMQ consumer** reads linked records continuously and **writes + merges** them into the
unified **kgdb** Postgres DB, for both `event` and `entity` categories (streaming). The whole
ontology maps onto existing kgdb tables — no new tables. Persistence contract: the *KG Database
Persistence* section of [`linking.md`](../linking.md) and
*KG entity extraction & linking integration* in
[`DATABASE_POSTGRES.md`](../../../../media-backend-paid/docs/DATABASE_POSTGRES.md), **corrected
to the live schema** (the docs are stale — see Live-schema facts).

## Step Zero (DONE)

A decoupled, idempotent **batch writer**: load an existing `data/linked/<stem>.json` and write
its events into the local **dev** kgdb — **create/upsert only, no in-DB merge**. It builds the
reusable `KgdbWriter` the streaming consumer will later call per message, so it's the
foundation, not a throwaway.

> **Implemented + validated on dev.** `src/entities/linking/persistence.py` (`KgdbWriter`) +
> `scripts/persist_linked.py`. The `geo_qro_paid_mass_event` fixture writes 463 entities, 926
> `entity_types`, 463 `event_properties` (slack-widened windows), 409 `entity_locations` (skipped
> where `_geo` absent), 953 `entities_documents` (= Σ `source_ids`); re-runs are no-ops by
> `_link_id`. The detailed spec below matches the implementation.

### Live-schema facts (verified via `local-kgunified-postgres-server` MCP)

- `entity_locations` live columns have **no `location_` prefix**: `record_id, entity_id,
  coords(point), formatted_name, precision_level(text), geoid, level_{1..7}, level_{1..7}_id`.
- **Schema bug (blocker for locations):** `entity_locations.entity_id` is `GENERATED ALWAYS AS
  IDENTITY`; `record_id` (PK) has **no** identity/default. Identity is on the wrong column.
- `entities_documents` live **has** `parent_doc_id` + `news_type` (`schema.sql` is behind).
- Auto-identity PKs fine on: `entities.entity_id`, `entities_alias.alias_id`,
  `entities_documents.ent_doc_id`, `entity_types.record_id`, `event_properties.record_id`.
- Unique keys for upsert: `entities_documents (entity_id, doc_id)`, `event_properties
  (event_id)`, `entities_alias (original_entity_id)`. **No** natural unique key on
  `entity_types` / `entity_locations` → writer dedups.
- Catalog **not seeded**: `entity_kinds_available` = {event, entity}; `entity_types_kinds_
  available` has only generic rows, no KG supertypes/child types, all `metadata_template` NULL.
- Linked events all carry `_supertype` + leaf `event_type` (catalog-resolvable).

### Prerequisites

> **Done (dev).** P1 + P2 are authored and applied to the local dev kgdb (`kgunified` on the
> Docker instance — see [`dev/docs/db/runbook.md`](../../../../dev/docs/db/runbook.md)).
> `schema.sql` + `inserts_catalog_tables.sql` were first re-dumped from the live DB so the
> baseline matches production (un-prefixed `entity_locations`, `entities_documents`
> `parent_doc_id`/`news_type`, the generic catalog). The standalone DDL still needs applying to
> **live** as a deliberate production step.

**P1 — fix `entity_locations` identity** ✅ — moved identity off `entity_id` onto `record_id`.
Standalone idempotent DDL `media-backend-paid/docs/kg_event_persistence_kgdb.sql` (safe on
populated live), **folded into** `media-backend-paid/db/kg_db/schema.sql`. The live-shape
reconciliation (corrected `entity_locations` names + `entities_documents`
`parent_doc_id`/`news_type`) was done via a fresh `pg_dump` of the live DB.

**P2 — seed the type catalog** (`entity_types_kinds_available`) ✅ — per event/entity supertype, a
supertype row (`entity_kind` from `meta.category`, `parent_entity_type=NULL`, `metadata_template`
= the schema JSON from `src/entities/extraction/schemas/{supertype}.json`) + a child row per leaf
type (parent = the supertype id). Supertype→child mapping from
`src/entities/extraction/catalogues/event_types.csv`. Includes `legislative_initiative`
(+children) so the `entity` category is ready; themes skipped (no `theme` kind yet). Generator
`scripts/gen_kg_catalog_seed.py` → `media-backend-paid/docs/kg_catalog_seed_kgdb.sql` (10
supertypes + 54 children). Upsert uses the existing live `UNIQUE (entity_type, entity_kind)`
constraint for `ON CONFLICT` re-runnability. The seeded catalog will also gain an `active` flag
as the source of truth for which types are extracted — designed in
[`active_type_extraction.md`](active_type_extraction.md).

### `KgdbWriter` (`src/entities/linking/persistence.py`)

`psycopg2` + `KGDB_*` env vars, **mirroring `scripts/build_customer_fixture.py`**
(`KGDB_HOST/PORT/USER/PASSWORD/NAME`, `RealDictCursor`, `execute_values`). Category-aware
(`event` now, `entity` ready). Surface:
- `write_linked(record) -> original_entity_id` — one record, one transaction (the unit the
  future consumer calls).
- `reset_run(run_tag)` — delete a prior run's rows (idempotency).

`write_linked` (per the corrected write-path sketch):
1. Resolve catalog ids: `_supertype` → supertype `entity_type_id`; `event_type` → child id
   (fall back to supertype-only). Drop+log if supertype unseeded.
2. **entities** `INSERT … RETURNING entity_id`. `metadata` = full linked JSON **+** `_link_id`
   (linker id, dedup key) **+** `_link_run` (stem). `added=now()`; keywords/embedding/filter null.
3. **entities_alias** `(original_entity_id, entity_alias, current_entity_id) = (entity_id, name,
   entity_id)`.
4. **entity_types** supertype row + child row (`entity_id = original_entity_id`).
5. **entity_locations** from `record["_geo"]`: `coords = point(matched_lon, matched_lat)`,
   `precision_level = str(...)`, `geoid`, `formatted_name`, `level_{1..7}`/`_id`. Skip if no
   `_geo`. (Single `_geo` now; multi-row once [list locations](location_level_list_extraction.md) lands.)
6. **event_properties** (events only) `ON CONFLICT (event_id) DO UPDATE`. Store the
   **slack-widened confidence window** as `date_start/date_end` (so a `tstzrange &&` index
   reproduces the candidate date filter); precise range + `precision_days` stay in
   `entities.metadata`. Entities get no `event_properties` row.
7. **entities_documents** one row per `source_ids[i]`: `(entity_id=original_entity_id,
   doc_id=source_id, doc_index='news', doc_date_created=publication_date, doc_source=host)`,
   `ON CONFLICT (entity_id, doc_id) DO NOTHING`.

`entity_id` written everywhere is `entities_alias.original_entity_id` (`= entity_id` at create).
Direct-FK caveat: `entity_locations.entity_id` / `event_properties.event_id` FK straight to
`entities.entity_id` — fine on create; the future merge step must rewrite them.

### Driver (`scripts/persist_linked.py`)
Loads `data/linked/<stem>.json` (`LINK_STEM`), opens `KgdbWriter`, optional `--reset`, calls
`write_linked` per event, prints counts + the `_link_id → entity_id` map. Loads `.env.local`.

### Idempotency
External key `entities.metadata->>'_link_id'` (stable within a linked JSON file): upsert by it.
`reset_run(stem)` deletes by `metadata->>'_link_run'=stem` in child→parent order (FKs are
direct). Re-running the persist script on the same file is a no-op/upsert; re-running the linker
changes ids → `--reset` first. (A [deterministic linked id](#deferred) removes this caveat.)

## Verification

- P1/P2 applied: MCP `describe_table entity_locations` (record_id now identity); catalog has
  supertypes+children, `paid_mass_event.metadata_template` non-null.
- `LINK_STEM=geo_qro_paid_mass_event python scripts/persist_linked.py --reset` → 463 entities.
- MCP counts: `entities WHERE metadata->>'_link_run'='geo_qro_paid_mass_event'` = 463;
  `entity_types` ≈ 2× events; `event_properties` = 463 with non-null dates; `entities_documents`
  ≈ Σ`source_ids`; spot-check Zona Fest rows + their location/property joins.
- Re-run without `--reset` → counts unchanged.

## Streaming consumer (RabbitMQ listener)

The production end-state for the `kg` worker (workspace map: **`kg` consumes a *doc queue* →
produces kgdb entities/events**). A long-lived worker that consumes **raw documents** and runs
the full pipeline **inline per message** — `classify → extract → link → persist` — i.e. the
[`run_entities.py`](../../src/entities/run_entities.py) loop (`match → extract → link_one →
upsert_linked`) lifted into a pika consumer callback. **No** intermediate "linked-records"
queue, and `rabbit_enqueuer` is producer-side only (not on the consume path).

> **Wrapper implemented:** [`src/listener.py`](../../src/listener.py) — `KgPipeline`
> (extract→link→persist, reusing the shared `record_to_article`) + `DocumentListener` (pika
> `BlockingConnection` consumer with retry/DLX), plus a `--once <fixture>` offline smoke mode.
> `KgdbWriter.upsert_linked` (added for streaming) updates the canonical row in place when the
> linker *merges* a new source in, and re-raises DB errors so the message requeues. The
> upsert path is validated against dev; the full extract→link→persist over a live broker
> needs OpenRouter + geocoder + the dev vhost. **Still pending: the kgdb-backed
> `CandidateIndex`** below (the real correctness blocker for restarts / multiple workers).

### Module & reuse (`src/listener.py`)

New module, modeled on the workspace's existing pika consumers
[`social_tags/src/stream.py`](../../../../social_tags/src/stream.py) and
[`ai_assist/src/stream.py`](../../../../ai_assist/src/stream.py), composing already-built
pieces (no reimplementation):

- `EntityExtractor.match(article)` / `.extract(article, validate=True,
  raise_validation_error=False)` (`extraction/extract.py`) — **restricted to active types**,
  see [`active_type_extraction.md`](active_type_extraction.md).
- `EntityLinker.link_one(raw) -> LinkResult` (`linking/link.py`) — the streaming entry point,
  already exception-wrapped.
- `KgdbWriter.upsert_linked(record)` (`linking/persistence.py`) — one record, one txn;
  create, or update-in-place on a linker merge (`write_linked` is the batch/Step-Zero variant).
- `record_to_article(record)` (`src/entities/document.py`) — map a doc envelope to the extractor's
  article dict; lift into a shared helper.

### Config & connection

`RabbitConfig.from_env()` mirroring [`social_tags/src/settings.py`](../../../../social_tags/src/settings.py):
`RABBIT_HOST/PORT/USER/PASSWORD/VIRTUALHOST/EXCHANGE/QUEUE/ROUTING_KEY`, plus
`RABBIT_PREFETCH_COUNT` (default 1), `RABBIT_RETRY_DELAY_SECONDS`, `RABBIT_MAX_RETRIES`,
`RABBIT_DLX`. DB creds reuse Step Zero's `KGDB_*`. Connect with `pika.BlockingConnection`
(heartbeat 600), declare durable exchange/queue, bind, `basic_qos(prefetch_count=1)`,
`basic_consume`, signal handlers for graceful shutdown.

### Per-message callback (= the `run_entities.py` loop)

Parse JSON body → document record; pull `trace_id` from the **message top level** (dev
convention — never inside the payload). **Document-level dedup first:** if the doc id
(`_id`/`url`) is already in the Redis `ProcessedStore` (`src/processed_store.py`, 2-week TTL),
**ack and skip** before any extraction; mark it processed only after a successful run. This is
the cheap idempotency layer (skips redelivered/re-enqueued docs entirely) on top of the linker's
kgdb dedup and the writer's `_link_id` upsert. Then `_record_to_article` → `match` (empty ⇒ ack,
nothing to do) → `extract` → per extracted `raw`: `link_one`, routed by `LinkResult.status`:

- `created` / `merged` → `writer.write_linked(result.record)`.
- `skipped` → category not linked yet (theme / entity-concept) → **not persisted today**;
  counted + logged (see the gap note below).
- `dropped` / `error` → logged + counted, message still acked (poison content, not transient).

**Ack/nack/retry** (mirror `social_tags/src/stream.py`): success ⇒ `ack`; transient failures
(kgdb / geocoder / OpenRouter unreachable) ⇒ `sleep(retry_delay)` then `nack(requeue=True)` up
to `max_retries`, then `nack(requeue=False)` → DLX; validation/poison ⇒ immediate
`nack(requeue=False)`. Honors the global rule: a depended-on service being unreachable is
**surfaced and requeued (bounded)**, never silently dropped.

### Hard prerequisite — kgdb-backed `CandidateIndex` (DONE)

> **Implemented** — see [`kgdb_candidate_index.md`](kgdb_candidate_index.md) (the full backend
> contract — CandidateIndex + record store — and the column-reconstruction retrieval design).
> The listener now dedups against kgdb across processes; validated cross-process on dev.

`link_one` resolves candidates against the in-memory `CandidateIndex` (`linker.events` /
`linker.index`), which `linking.md` marks "in-memory today, **kgdb-backed later**". A
batch writer (Step Zero) builds that index in-process for one file and exits; a streaming
worker must share candidate state across messages, restarts, and parallel workers ⇒ a
**kgdb-backed `CandidateIndex`** (querying `event_properties` / `entity_locations` /
`entity_types` per the readme lookup contract) is the real blocker for streaming, on top of
the linking-quality sequencing above.

### Persisting extracted non-event entities — current gap

The write path stores **linked canonical *events* only**. Per-document raw extractions are
**not** their own rows — each source survives as an `entities_documents` row (one per
`source_id`) plus the merged `entities.metadata`; `data/extracted_raw/` is debug-only.

- **Events** (`category=event`): linked → persisted (Step Zero + write path). ✅
- **Entities/concepts** (`category=entity`): `KgdbWriter` is entity-ready, but the linker
  **skips** `category=entity`, so they never reach the writer → Deferred *Entities/concepts*.
- **Themes** (`category=theme`): **no write path** at all → Deferred *Themes*.

So the listener routes `skipped` records to a counted no-op today; closing the gap is exactly
the two Deferred items below.

### Local dev loop (own local kgdb)

Run the worker on the dev vhost `document_processing_dev` against a doc queue (e.g.
`dev_kg_documents`); publish a sample document with a `dev/send_*.sh`-style script
(`pika.BlockingConnection`, `delivery_mode=2`, top-level `trace_id`); reconstruct with a
per-service logfile + `dev/trace.sh <trace_id>`. **The dev environment must stand up its own
local Postgres kg database** (own instance + `media-backend-paid/db/kg_db/schema.sql` + the
P1/P2 migrations applied), with the dev `KGDB_*` pointing the worker/`KgdbWriter` at it — so
streaming writes never touch the current/real kgdb. This generalizes the existing
`media-backend-paid → rabbit_enqueuer → event_report` dev loop to the **kg → kgdb** chain.

## Deferred (subsequent steps)

- **Streaming consumer** — **implemented** (`src/listener.py`) and validated; the kgdb-backed
  `CandidateIndex` + record store ([`kgdb_candidate_index.md`](kgdb_candidate_index.md)) and the
  local dev kgdb are both done. Remaining: live broker round-trip at scale + the multi-worker race
  backstop (in-DB merge below).
- **In-DB merge** — alias `current_entity_id` repoint + direct-FK fixup on
  `entity_locations`/`event_properties` ([`canonical_reconciliation.md`](canonical_reconciliation.md)).
- **Themes** — add `theme` to `entity_kinds_available` + degenerate single-entity write path.
- **Entities/concepts** — exercised once the linker stops skipping `category=entity`.
- **Deterministic linked id** — content-hash suffix instead of random; removes the idempotency
  caveat and fixes the link-LLM cache misses.
