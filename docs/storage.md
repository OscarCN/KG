# Storage — kgdb persistence & streaming pipeline

Single source of truth for how the KG pipeline persists into the unified **kgdb**
Postgres database and how the production streaming consumer runs. The extraction and
linking subsystems that feed this are documented in [extraction.md](extraction.md) and
[linking.md](linking.md); the broader pipeline overview is in
[architecture.md](architecture.md). The full kgdb schema and cross-database
conventions live in
[`media-backend-paid/docs/DATABASE_POSTGRES.md`](../../../media-backend-paid/docs/DATABASE_POSTGRES.md).

> **Status: streaming + kgdb-backed retrieval implemented (validated on dev).**
> [`../src/entities/linking/persistence.py`](../src/entities/linking/persistence.py)
> (`KgdbWriter`) writes linked records into kgdb following the model below — in batch
> via [`../scripts/persist_linked.py`](../scripts/persist_linked.py) from a
> `data/linked/<stem>.json` fixture (validated on dev: the `geo_qro_paid_mass_event`
> fixture — 463 entities + 926 `entity_types` + 463 `event_properties` + 953
> `entities_documents`, idempotent re-runs) and inline per message by the **streaming
> RabbitMQ consumer** [`../src/listener.py`](../src/listener.py) (consume documents →
> extract → link → `KgdbWriter.upsert_linked`). The consumer uses kgdb-backed candidate
> retrieval ([`../src/entities/linking/kgdb_retrieval.py`](../src/entities/linking/kgdb_retrieval.py):
> `KgdbCandidateIndex`/`KgdbRecordStore`), so dedup holds across restarts and workers
> (validated on dev over multi-hundred-document batches). **Still pending:** the in-DB
> canonical↔canonical merge (reconciliation) and the production producer/retriever — see
> [`todos/kgdb_event_persistence.md`](todos/kgdb_event_persistence.md). **Residual risk
> (honest):** candidate-lookup → adjudicate → create runs in the linker *outside* any DB
> lock, so under true multi-worker parallelism duplicate canonicals are still possible
> (a writer-only lock can't fix it) — covered by
> [`todos/canonical_reconciliation.md`](todos/canonical_reconciliation.md).

## Streaming pipeline

The production shape: a long-lived worker that consumes **raw documents** off a RabbitMQ
queue and runs the full pipeline **inline per message** — `classify → extract → link →
persist` — writing canonical events into kgdb. Implemented and validated on dev
(multi-hundred-document live batches), robust for a single long-lived worker and now
deduping across restarts via kgdb-backed retrieval. The remaining open work is the **in-DB
canonical↔canonical merge** (reconciliation) and the production **producer/retriever**.

### Streaming consumer (`src/listener.py`)

A `pika` `BlockingConnection` consumer (modeled on the workspace's `social_tags`/`ai_assist`
workers): durable queue, `prefetch=1`, dead-letter exchange, bounded retry→requeue,
`trace_id` at the message top level, graceful shutdown. Per message it does document-level
dedup first (see [Idempotency](#three-idempotency-layers) below), then `record_to_article →
Ontology.match` (the keyword pre-filter; non-matches are dropped) `→ extract → link_one →
KgdbWriter.upsert_linked`. Scale by running N listeners — cross-worker dedup holds. A
`--once <fixture>` mode runs the same pipeline offline (no broker). Producers today are
test-only: [`../scripts/enqueue_from_es.py`](../scripts/enqueue_from_es.py) (ES date-window
fetch → doc queue) and [`../scripts/publish_document.py`](../scripts/publish_document.py);
the eventual global retriever is the open producer-side work
([`todos/document_retrieval_strategy.md`](todos/document_retrieval_strategy.md)).

### kgdb-backed retrieval (`src/entities/linking/kgdb_retrieval.py`)

So dedup works **across restarts and multiple workers**, candidate lookup reads from kgdb
instead of per-process memory.
[`../src/entities/linking/index.py`](../src/entities/linking/index.py) defines two swappable
backends — `CandidateIndex` (id retrieval) and `RecordStore` (id→record). The in-memory pair
is used for batch/test runs; the kgdb pair for streaming: `KgdbCandidateIndex` reconstructs
the candidate set with one SQL query over the rows the writer already persists
(`event_properties` date `&&`, `entity_locations` level ids / grid block, `entity_types`
supertype), and `KgdbRecordStore` resolves records from `entities.metadata`. The hard geo
gate / deterministic gate / LLM still run on the resolved records.

These retrieval predicates are backed by indexes in the kgdb schema (expression indexes on
`entities (metadata->>'_link_id')` and `(metadata->>'_supertype')`, a GiST on the
`event_properties` date range, btrees on `entity_locations.level_N_id`, GiST on coords;
asserted by [`../src/entities/linking/test_kgdb_indexes.py`](../src/entities/linking/test_kgdb_indexes.py)),
so the lookups don't scan.

### Merge mechanism

The same real-world event reported across many news sites collapses into **one canonical
entity**: on a match the linker merges (most-precise date window and highest-precision
location win; `source_ids` grows de-duped), and `upsert_linked` updates the kgdb row in place
— refreshing `entities.metadata`, adding the new per-source `entities_documents` row (each
source's OWN publication date and `news_type`), and updating the `event_properties` window.
The in-place update locks the row (`SELECT … FOR UPDATE`) and UNIONs the DB's
`source_ids`/`_source_windows`/`_sources` accumulators before writing, so concurrent merges
into the same canonical are additive (no lost sources). (Observed live: a single festival
merged from 26 distinct sources.)

### Three idempotency layers

Cheapest first:

1. **Redis atomic claim** — [`../src/processed_store.py`](../src/processed_store.py)
   (`ProcessedStore`) takes an atomic in-flight claim (`SET NX EX` on a short-TTL
   `kg:processing:<id>` key) before any extraction and rejects docs already marked processed
   or claimed by another worker, so concurrent duplicate deliveries can't both pass; on
   success it sets the long-TTL processed marker and clears the claim, on retryable failure it
   releases the claim, and dead-letter lets the claim expire by TTL.
2. **Linker kgdb dedup** — a redelivered/duplicate document re-matches its existing event and
   merges rather than duplicating.
3. **Writer `_link_id` upsert** — the streaming `upsert_linked` finds an existing canonical by
   `metadata->>'_link_id'` **alone** (run-tag-independent), so a new run or backfill merges
   into the existing row instead of duplicating; the batch `write_linked` stays run-scoped
   (`_link_id` + `_link_run`) for per-run idempotency and `reset_run(tag)`.

### Per-document extractions (`document_extractions`)

Alongside the merged canonical, the listener persists **every extracted record before the
merge** (`KgdbWriter.write_extraction`), one row each, *including the ones the linker
skips/drops/errors* (themes, entities, no-date) which produce no `entities_documents` row.
Each row carries the validated record JSON, provenance (`extraction_model`, `prompt_variant`,
`run_tag`), and a nullable `linked_entity_id` (the canonical it folded into). Idempotent on
`(doc_id, record_hash)`. **Why it earns a table:** it's the pre-merge ground truth — it lets
us **re-link without re-paying the LLM** when linking logic changes, preserves what each
source actually said before fillna/most-precise-wins collapses it, and gives training/debug
data for the over/under-merge cases. See
[`todos/persist_document_extractions.md`](todos/persist_document_extractions.md).

### Ontology rules in kgdb (`ontology_matching_rules`)

The keyword/category matching rules (the in-worker `Ontology.match` pre-filter) can live in
kgdb instead of `keywords.xlsx`, selected by `KG_ONTOLOGY_SOURCE` (`xlsx` default for
dev/test, `db` for production). The table stores rules **raw and human-editable** (one row per
rule, list columns + `enabled` + labels); `Ontology` normalizes them **at load**, identically
to the Excel parse, so matching is **byte-identical** across sources (verified: same enabled
classes, rule set, and `match()` output). **Why:** a single queryable, editable source of
truth for production matching (no Excel redeploy to change a keyword), versionable per row, and
the natural home for the future `active`-type gate. Seeded from the Excel by
[`../scripts/seed_ontology_rules.py`](../scripts/seed_ontology_rules.py) (full refresh).

### Configuration

RabbitMQ (`RABBIT_HOST/PORT/USER/PASSWORD/VIRTUALHOST/QUEUE`, `RABBIT_PREFETCH_COUNT`,
`RABBIT_DLX`), kgdb (`KGDB_HOST/PORT/USER/PASSWORD/NAME`), Redis (`REDIS_HOST/PORT/PASSWORD`,
`KG_PROCESSED_TTL_SECONDS`, `KG_PROCESSING_TTL_SECONDS` — short in-flight claim TTL, default
600s), `KG_RUN_TAG` (provenance), `KG_ONTOLOGY_SOURCE` (`xlsx` default / `db` to load matching
rules from `ontology_matching_rules`), plus OpenRouter + geocoder. The listener loads
`kg/.env.local`. The local **dev kgdb** (Docker Postgres on `:5334`) setup is in
[`media/dev/docs/db/runbook.md`](../../../dev/docs/db/runbook.md).

## Persistence model

[`../src/entities/linking/persistence.py`](../src/entities/linking/persistence.py)
(`KgdbWriter`) writes a linked record into kgdb as one transaction per record (the unit the
streaming consumer calls per message). The whole KG ontology is encoded in the **existing
kgdb tables** — no new tables are introduced for the ontology itself.

| KG concept | kgdb table | Role |
|---|---|---|
| Category (`event` / `entity` / `theme`) | `entity_kinds_available` | Top-level enumeration; maps 1:1 to `meta.category` |
| Supertype (e.g. `paid_mass_event`) | `entity_types_kinds_available` (row with `parent_entity_type = NULL`) | Holds the JSON schema in `metadata_template` |
| Child type (e.g. `concert`) | `entity_types_kinds_available` (row with `parent_entity_type = <supertype id>`) | Inherits the parent's schema (`metadata_template = NULL`) |
| Linked record | `entities` | One row per canonical entity, validated record in `metadata` JSON |
| Alias / dedup pointer | `entities_alias` | `original_entity_id` is the stable external key; `current_entity_id` points at the surviving entity after a merge |
| Type membership | `entity_types` | Associates the entity with its supertype and (when known) child type |
| Location | `entity_locations` | One row per geocoded `Location`; schema mirrors the geocoder output |
| Source-document linkage | `entities_documents` | One row per `(entity, doc)` pair the linked entity is mentioned in |
| Event linking lookups | `event_properties` | Materialised `date_start`, `date_end`, `status`, `status_date` to avoid scanning `entities.metadata` JSON |

### Categories (`entity_kinds_available`)

The KG ontology has three top-level categories — **event**, **entity**, **theme** — declared
on every supertype schema as `meta.category`. They map 1:1 to rows in
`entity_kinds_available`. Currently the table contains `event` and `entity`; **`theme` will be
added when theme rows start being written**.

### Supertypes and types (`entity_types_kinds_available`)

The KG ontology is two-level: a **supertype** (e.g. `paid_mass_event`,
`legislative_initiative`, `security`) defines the JSON schema, and one or more **child types**
(e.g. `concert`, `festival`; `law_initiative`, `decree`; `crime_trends`, `law_enforcement`)
refine it. Both live as rows in `entity_types_kinds_available`, distinguished by
`parent_entity_type`:

| Row | `entity_kind` | `parent_entity_type` | `metadata_template` |
|---|---|---|---|
| Supertype | `event` / `entity` / `theme` | `NULL` | The full JSON schema (= the contents of `../src/entities/extraction/schemas/{supertype}.json`) |
| Child type | same as parent's | The supertype's `entity_type_id` | `NULL` (inherits the parent's schema) |

The supertype's `metadata_template` is seeded by
[`../scripts/gen_kg_catalog_seed.py`](../scripts/gen_kg_catalog_seed.py).

**Inheritance scope** is intentionally limited to supertype → child type. Children do not
currently override or extend the supertype schema — extracted records for any child of
`paid_mass_event` are validated against `paid_mass_event.json` regardless of which child class
produced them. Storing the schema only on the supertype keeps a single source of truth and
avoids fan-out updates across every child row when a schema evolves. If/when child-level field
overrides are needed, the child row's `metadata_template` will carry the delta (extra fields),
and validation will need to merge parent + child schemas before parsing.

### Canonical records (`entities`, `entity_types`)

Every linked output of the pipeline becomes one row in `entities`:

- `name`, `description`, `keywords`, `embedding`, `filter_llm_prompt` — populated from the
  linked record (or left blank for now where the pipeline doesn't produce them).
- `metadata` (`json`) — the validated, schema-conformant extracted record (output of the
  schema `Parser` for the supertype) **+** `_link_id`/`_link_run` provenance. Shape depends on
  the supertype:
  - `paid_mass_event`: `event_type`, `date_range`, `location`, `price_range`, `attendance`, …
  - `legislative_initiative`: `entity_type`, `name`, `jurisdiction`, `date_introduced`,
    `legislative_body`, …
  - theme supertypes: `theme_type`, optional `location`, plus per-article fields (see *Themes*
    below)

The entity is associated with its supertype (and, when known, the child type) via
`entity_types` rows pointing at `entity_types_kinds_available.entity_type_id`.
`entity_types.entity_id` references `entities_alias.original_entity_id`, so entity merges remain
stable across the indirection layer.

> **Future: multi-class entities.** Today the linker writes one supertype (+ optional child
> type) per entity, but `entity_types` is already a many-to-many — a single `entity_id` can
> carry multiple `entity_type_id` rows. An entity instantiating more than one ontology class
> simultaneously (e.g. an event that's both a `paid_mass_event` and a `protest_event`, or a
> `legislative_initiative` that also acts as a `security` theme anchor) is a real possibility
> we'll address when inheritance work properly lands. The schema accommodates it; the open
> questions are at the validation layer (which class's schema does `entities.metadata` conform
> to?) and at the linker (does multi-class change the candidate filter?). Until inheritance is
> tackled, treat one supertype per entity as the working assumption.

### Themes are degenerate single entities

A theme is a topical classifier — every article matching `(theme_class, location_up_to_level_3)`
should link to the **same** `entities` row. The KG never produces a unique "instance" of a
theme; instead, the linker maintains one canonical theme entity per
`(theme_supertype_or_child_type, level_1, level_2, level_3)` tuple and appends
`entities_documents` rows for each matching article.

Consequently, the theme schema's article-side fields (`description`, `tags`, `context`,
`relevance`, `sentiment`, `related_subtopics`, `time_scope`) describe a particular article's
take on the theme, not a stable property of the canonical theme entity. **Recommendation:** for
theme rows, keep `entities.metadata` minimal — only `theme_type`/`theme_subtype` and the
location reference. Per-article variations belong on the `entities_documents` link, not on the
canonical entity. (Per-article sentiment already has a home in `entities_documents_sentiments`.)

### Locations (`entity_locations`)

Events, and some entities, carry one or more rows in `entity_locations`. The `entity_locations`
schema is intentionally aligned with the deepriver geocoder output (see
[`../src/entities/linking/geocode.py`](../src/entities/linking/geocode.py)):

| `entity_locations` column | Geocoder field |
|---|---|
| `coords` (`point`) | `(matched_lon, matched_lat)` |
| `formatted_name` | `formatted_name` |
| `precision_level` | `precision_level` (1–7) |
| `geoid` | `geoid` |
| `level_{N}` / `level_{N}_id` | `level_N` for N=1..7 (1=country, 2=state, 3=city, 5=neighborhood, 6=street, 7=place) |

Multiple locations per entity are allowed (one row per location). Themes are the only category
whose canonical-entity identity *requires* a coarse location (up to level 3) — see *Themes are
degenerate single entities* above.

### Linking lookups (`event_properties`)

`event_properties` is the index that the linker uses to find candidate matches for a new
incoming event without parsing every `entities.metadata` JSON blob. It carries the fields the
candidate filter needs:

| Filter dimension | Source for an incoming event | Stored on the linked event row |
|---|---|---|
| Date overlap | `metadata.date_range.date_range.{start,end}` | `event_properties.date_start`, `event_properties.date_end` |
| Geographic prefix | geocoded `level_2_id` of the event's location | `entity_locations.level_2_id` |
| Type compatibility | `metadata.event_type` (resolved to `entity_type_id`) | `entity_types.entity_type_id` |

Without `event_properties`, the linker would have to extract `date_start`/`date_end` from each
candidate's `metadata` JSON at query time — a JSON-path scan over all events of the right type
and area. Materialising them as columns lets a normal range index drive candidate retrieval.

`status` and `status_date` track event lifecycle (e.g. an `arrest_event` moving from
`reported` → `confirmed` → `closed`) without rewriting `metadata` — useful when only the
lifecycle changed but the underlying extracted record is unchanged.

#### Open question: fold `event_properties` into `entities`?

The fields are small (3 timestamps, 1 status string) and tightly coupled to event rows, so
folding them in would save a join.

**Recommendation: keep `event_properties` separate.** Three reasons:

1. `entities` is shared across all three categories. Event-only columns on it would be `NULL`
   for ~⅔ of rows (themes and entities/concepts) and grow as new event-only or entity-only
   properties emerge — the table heads toward a sparse heterogeneous schema. The same pattern
   (typed property tables alongside `entities`) generalises to future categories — e.g. an
   analogous property table for legislative initiatives carrying `date_introduced`,
   `voting_status`, etc.
2. Status updates are far more frequent than full-record rewrites. Keeping them off `entities`
   means status churn doesn't dirty the canonical row (and its embedding/keywords/metadata) on
   every state transition, and doesn't compete for autovacuum on a much larger table.
3. The linker's join cost is bounded by `(entity_type_id, level_2_id, date overlap)`, all
   highly selective; a covering index on `event_properties (date_start, date_end)` plus the
   existing `event_id` constraint keeps the candidate fetch cheap.

The kgdb candidate-retrieval predicates
([`../src/entities/linking/kgdb_retrieval.py`](../src/entities/linking/kgdb_retrieval.py)) are
backed by indexes in the schema (`media-backend-paid/db/kg_db/schema.sql`, asserted by
[`../src/entities/linking/test_kgdb_indexes.py`](../src/entities/linking/test_kgdb_indexes.py)):
expression indexes on `entities (metadata->>'_link_id')` and `(metadata->>'_supertype')`, a
GiST on the `event_properties` date range, btrees on `entity_locations.level_N_id`, and a GiST
on coords. So the date/geo/type/`_link_id` lookups don't scan. Denormalising `event_properties`
into `entities` remains unnecessary.

### Write path (`KgdbWriter.write_linked` / `upsert_linked`)

`KgdbWriter` implements the steps below — one transaction per linked record. Themes are not
linked upstream, so the theme branch in step 1 is not exercised yet. For each linked record:

1. Resolve or create the entity:
   - For events/entities: insert a new `entities` row (`metadata` = the validated record **+**
     `_link_id`/`_link_run` provenance), then an `entities_alias` row with
     `original_entity_id = current_entity_id = entities.entity_id`.
   - *(Planned)* For themes: look up the canonical `(theme_type, level_1, level_2, level_3)`
     entity and reuse its `entity_id`, or create one if absent.
2. Insert `entity_types` rows linking the entity (via `entities_alias.original_entity_id`) to
   the supertype's `entity_type_id` and, when the leaf resolves, the child type's.
3. For the geocoded location (`record["_geo"]`), insert an `entity_locations` row with the
   geocoder's level breakdown (skipped when no `_geo`). One row today; multi-row once
   [list locations](todos/location_level_list_extraction.md) land.
4. For events, upsert the `event_properties` row (`ON CONFLICT (event_id)`) with the
   **slack-widened** `date_start`/`date_end` (so a `tstzrange &&` index reproduces the
   candidate date filter) and `status`.
5. For each `_sources` entry, upsert `entities_documents (entity_id, doc_id)`
   (`doc_index='news'`) carrying that source's OWN `doc_date_created` (its `publication_date`)
   and `news_type` — not the canonical earliest date — with `doc_source`=host, org-agnostic,
   per the existing sentiment write path. (Old records without `_sources` fall back to
   `source_ids` + the canonical date.)

`entity_id` everywhere is `entities_alias.original_entity_id` (== `entity_id` at create), so
later entity merges don't break the link.

**Idempotency.** The batch path (`write_linked`) is run-scoped: records already written under
the run (`metadata->>'_link_id'` **+** `_link_run`) are skipped, and `KgdbWriter.reset_run()`
deletes a run (child→parent order) for a clean re-write. The streaming path (`upsert_linked`)
matches an existing canonical by `metadata->>'_link_id'` **alone** (run-tag-independent), so a
new run or backfill merges into an existing canonical instead of duplicating it.

**Concurrent merges are additive.** The in-place update locks the canonical row
(`SELECT … FOR UPDATE`) and UNIONs the DB's `source_ids` / `_source_windows` / `_sources`
accumulators with the incoming record before writing, so two workers merging different sources
into the same canonical don't clobber each other's source accumulators (no last-writer-wins
loss).

Drop buckets: `no_supertype`, `unseeded_supertype:<name>`, `error`.

### Direct-FK exception (recap)

`event_properties.event_id`, `entity_locations.entity_id`, and `relations.ent_id_*` FK directly
to `entities.entity_id` rather than `entities_alias.original_entity_id` (a known oversight
pending migration). Until that migration lands, the pipeline must take care to write to the
surviving `entities.entity_id` (resolve the alias indirection before insert) and entity-merge
logic must rewrite these rows. See the *Cross-database references* and *Exceptions* sections of
[`DATABASE_POSTGRES.md`](../../../media-backend-paid/docs/DATABASE_POSTGRES.md) for the
canonical write.
