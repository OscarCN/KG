# TODO — Persist linked events into kgdb (Step Zero → streaming write/merge)

**Status:** open — design ready; **sequenced after** the retrieval/linking improvements below
**Area:** new `src/entities/linking/persistence.py`, `scripts/persist_linked.py`, `scripts/gen_kg_catalog_seed.py`; kgdb migrations in `media-backend-paid/db/kg_db/`
**Related:** [`canonical_reconciliation.md`](canonical_reconciliation.md), [`retrieval_name_soft_type.md`](retrieval_name_soft_type.md), [`location_level_list_extraction.md`](location_level_list_extraction.md), [`../../src/entities/linking/readme_linking.md`](../../src/entities/linking/readme_linking.md), [`../../../../media-backend-paid/docs/DATABASE_POSTGRES.md`](../../../../media-backend-paid/docs/DATABASE_POSTGRES.md)

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
Persistence* section of [`readme_linking.md`](../../src/entities/linking/readme_linking.md) and
*KG entity extraction & linking integration* in
[`DATABASE_POSTGRES.md`](../../../../media-backend-paid/docs/DATABASE_POSTGRES.md), **corrected
to the live schema** (the docs are stale — see Live-schema facts).

## Step Zero (first attempt)

A decoupled, idempotent **batch writer**: load an existing `data/linked/<stem>.json` and write
its events into the local **dev** kgdb — **create/upsert only, no in-DB merge**. It builds the
reusable `KgdbWriter` the streaming consumer will later call per message, so it's the
foundation, not a throwaway.

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

**P1 — fix `entity_locations` identity** (migration on dev kgdb): move identity off `entity_id`
onto `record_id`; fold the live shape (corrected `entity_locations` names + `entities_documents`
`parent_doc_id`/`news_type`) back into `media-backend-paid/db/kg_db/schema.sql`. New DDL:
`media-backend-paid/docs/kg_event_persistence_kgdb.sql`.

**P2 — seed the type catalog** (`entity_types_kinds_available`): per event supertype, a supertype
row (`entity_kind='event'`, `parent_entity_type=NULL`, `metadata_template` = the schema JSON from
`src/entities/extraction/schemas/{supertype}.json`) + a child row per leaf type (parent = the
supertype id). Supertype→child mapping from
`src/entities/extraction/catalogues/event_types.csv`. Seed `legislative_initiative` (+children)
too so the `entity` category is ready. Generator: `scripts/gen_kg_catalog_seed.py` →
`media-backend-paid/docs/kg_catalog_seed_kgdb.sql`. (Recommend `UNIQUE (entity_type,
parent_entity_type)` for `ON CONFLICT` re-runnability.)

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

## Deferred (subsequent steps)

- **Streaming consumer** — RabbitMQ worker calling `KgdbWriter.write_linked` per message.
- **In-DB merge** — alias `current_entity_id` repoint + direct-FK fixup on
  `entity_locations`/`event_properties` ([`canonical_reconciliation.md`](canonical_reconciliation.md)).
- **Themes** — add `theme` to `entity_kinds_available` + degenerate single-entity write path.
- **Entities/concepts** — exercised once the linker stops skipping `category=entity`.
- **Deterministic linked id** — content-hash suffix instead of random; removes the idempotency
  caveat and fixes the link-LLM cache misses.
