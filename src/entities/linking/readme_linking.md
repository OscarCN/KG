# Entities — Linking

Deduplicates and merges extracted **events** (the output of `../extraction/`) into canonical event records, each carrying a `source_ids` list of every document that mentions it. Future versions will link entities/concepts and themes too — see [KG Database Persistence](#kg-database-persistence) below for the persistence model that already accounts for them.

For an overview of the broader pipeline and ontology categories, see [`../readme_entities.md`](../readme_entities.md).

## Directory Structure

```
linking/
  geocode.py              # Geocoder wrapper (structured Location → level_2_id, coords, geoid)
  link_llm.py             # LLM disambiguator (gemini-2.5-flash-lite) with file cache
  link.py                 # EntityLinker: candidate filter + LLM call (events only). Exposes link_one(raw) → LinkResult for streaming callers.
  run_linking.py          # IPython runner — streams articles through the linker and (optionally) the tags pipeline.
  readme_linking.md       # This file
```

The runner streams extracted records grouped by `_source_id`, fetches each article's comments from the ES `news` index (or a local file in test mode), invokes `EntityLinker.link_one(raw)` per record, and — when [`TAGS_ENABLED`](#tagging-integration) — hands the `(linked event, article, comments)` triple to the tagging pipeline. See [`../tags/readme_tags.md`](../tags/readme_tags.md) for the tagging side.

## Linking Pipeline

**Scope: events only.** Records whose schema `meta.category != "event"` (themes, entities/concepts) are skipped by this version of the linker — they're tallied under `linker.dropped["skipped_category:..."]` and can be revisited later.

```
new event → schema parse → geocode location → candidate filter
                                              (event_type ∧
                                               date overlap ∧
                                               same level_2_id)
          → LLM disambiguation (gemini-2.5-flash-lite)
          → match-id ? merge : create new
```

### Candidate filter

For each incoming event, candidates are the already-linked events sharing **all three** of:

- same `event_type`
- same geocoded `level_2_id` (state). Events that geocode to no `level_2_id` are bucketed together under the empty-string key.
- date-range overlap with **slack** applied symmetrically:
  - **±1 day** when the incoming event has an extracted `date_range`. So two extracted-dated events match in the date dimension when their day windows are at most 2 days apart.
  - **±2 days** when the incoming event has no extracted date and falls back to its `_publication_date`. So two publication-only events match when their publication dates are at most 4 days apart.

Each linked event is registered in the candidate index under both its extracted-date window (when present, with extracted slack) and its publication-date window (when present, with publication slack). That way the next incoming event finds it regardless of which date source it carries.

The filter is intentionally broad — the LLM does the actual same-vs-different judgment.

### Date sources

Each extracted record may carry two date provenance fields:

| Field | Source | Used when |
|---|---|---|
| `date_range.date_range.{start,end}` | LLM-extracted from the article body | The article explicitly mentions when the event happened |
| `date_created` | Article publication timestamp, attached by `extract.py` (and copyable from ES via `src/PoC/enrich_extracted_raw.py`) | Falls back to this when no date is extracted |

Records with neither are dropped (`linker.dropped["event_no_date_no_pub"]`). When both are present, the extracted date_range wins for candidate-window resolution; the publication date is still kept for index registration so future publication-only records can find this event.

The linked record carries `publication_date` (the **earliest** publication date seen across merged sources) — useful as a stable temporal anchor when the extracted date_range is missing or imprecise.

### LLM disambiguation (`link_llm.py`)

A single LLM call decides whether the incoming event matches any candidate. The model is `google/gemini-2.5-flash-lite` (override via `OPENROUTER_LINKER_MODEL`). The payload sent to the LLM contains, for the incoming event and each candidate, ONLY:

| Field | Source |
|---|---|
| `name` | record `name` |
| `description` | record `description` |
| `address` | the structured `location` dict (country, state, city, neighborhood, zone, street, number, place_name) — **not** the whole event record |
| `date` | `{start, end}` from `record.date_range.date_range`, ISO-formatted (may both be null) |
| `publication_date` | ISO-formatted publication timestamp (when present); used by the LLM as a fallback temporal anchor |

Candidates additionally carry their `id`. The LLM is instructed to return either `{"match_id": "<one of the candidate ids>"}` or `{"match_id": null}`. Any id not present in the candidate list is treated defensively as `null`. Empty candidate lists short-circuit to `null` without an LLM call.

Responses are cached as `cache/link_llm/<sha256>.json`, keyed by `sha256(canonical(payload))`. Re-runs hit the cache and produce identical decisions without re-billing.

### Merge behavior

When the LLM picks a match, the linker:

- appends the new record's `_source_id` to the canonical event's `source_ids` (de-duped),
- fills nulls on `name`, `description`, `context`, `status` from the new record,
- widens `date_range.date_range` (`start = min`, `end = max`) and re-registers the event in the candidate index for any new day-keys the widened range introduced,
- promotes the canonical record's `location` to the new one when it has more populated subfields **and** belongs to the same `level_2_id`.

When no match is found, a new linked event is minted with id `{YYYYMMDD}_{level_2_id_or_noloc}_{rand}`.

### Drop reasons

| Bucket | Why |
|---|---|
| `skipped_category:<theme\|entity\|...>` | Record's schema is not an event — out of scope for this version |
| `event_no_type` | Record has no `event_type` |
| `event_no_date_no_pub` | Record has neither a parseable `date_range.date_range.{start,end}` nor a `date_created` |
| `no_supertype` | Record is missing the `_supertype` provenance field |
| `error` | Unhandled exception (logged with traceback) |

### Geocoder integration (`geocode.py`)

`geocode_location(loc)` consumes a structured `Location` dict (parsed `country, state, city, neighborhood, zone, street, number, place_name`) and feeds it to deepriver's geocoder via the structured-input path of `format_mentions` (which short-circuits the NLP step when its `main` argument is already a dict).

| Location field | Geocoder level key | Level # |
|---|---|---|
| `country` | `PAIS` | 1 |
| `state` | `EST` | 2 |
| `city` | `MUN` | 3 |
| `neighborhood`, `zone` | `COL` | 5 |
| `street` (+ `number`) | `CALLE` | 6 |
| `place_name` | `LUG` | 7 |

The geocoder is deepriver's own NLP + geocoding microservice pair, reached via `NLP_URL` and `GEOCODING_URL` env vars. The wrapper picks the highest-precision match from context group `'1'` of the response and exposes `geoid`, `precision_level` (int 1–7), `formatted_name`, `level_2/3/5/7`, `matched_lat`, `matched_lon`. Results are cached as JSON under `cache/geocode/<sha256>.json` keyed by the canonicalized Location dict, so re-runs avoid hitting the geocoding service.

### Output record shape

Each linked event extends the original schema with these link-level fields:

| Field | Type | Description |
|---|---|---|
| `id` | str | Minted linked id (`{YYYYMMDD}_{level_2_id_or_noloc}_{rand}`) |
| `source_ids` | List[str] | `_source_id`s of every document that mentions this event |
| `publication_date` | str | Earliest publication timestamp across the merged sources (when any source had one) |

Linked events also carry the merged `date_range` (widened on each overlap), the most-populated `location` fields seen across sources within the same `level_2_id`, and the `_geo` block from the geocoder.

### Running

`run_linking.py` is a step-by-step IPython script (mirrors `src/PoC/run_extraction.py`). Edit the `INPUT`, `OUTPUT`, and `GEOCODE` constants at the top of the file, then:

```bash
ipython src/entities/linking/run_linking.py
# or from a Jupyter/IPython session:
%run src/entities/linking/run_linking.py
```

After it finishes, the following names are bound for inspection:

| Name | What it holds |
|---|---|
| `records` | Raw extracted records loaded from `INPUT` |
| `linker` | The `EntityLinker` instance (with `linker.dropped`, `linker.events`, ...) |
| `linked` | Dict with an `events` list (themes/entities are skipped) |
| `stance_catalog` | (only when `TAGS_ENABLED`) The customer's `StanceCatalog` after the run |
| `claim_catalogs` | (only when `TAGS_ENABLED`) `ClaimCatalogRegistry` keyed on `(customer_id, event_id)` |
| `stats` | (only when `TAGS_ENABLED`) `StreamingStats` with the full per-phase counters |

## Tagging integration

When `TAGS_ENABLED = True` (default), the runner additionally drives the [tags subsystem](../tags/readme_tags.md): customer-anchored stances + per-event claim clusters. The flow per article is:

1. Fetch `(article, comments)` via `Retrieval.get_article_with_comments(_source_id)` (ES `news` index, or `LocalFileRetrieval` against a local file in test mode).
2. Stream each extracted record through `EntityLinker.link_one(raw)` — returns a `LinkResult(status, event_id, record, reason)`. `status` is `created` / `merged` / `skipped` / `dropped` / `error`.
3. For each `LinkResult` with `status ∈ {"created","merged"}`: tag the article + comments via Phase 2 (`TaggingOrchestrator.tag_batch`), adjudicate stance proposals if any (Phase 3), cluster claims if any (Phase 4), apply via `tags.apply` (Phase 5).
4. Print a per-article snapshot (top-N stances + new-cluster count) and an extra block whenever a new event is created.

After the run, in addition to `data/linked/<input>.json`, the runner writes `data/tags/<customer_slug>/run_<ts>.json` — a snapshot of the stance catalog (entries + assignments) and the per-event claim catalogs (clusters + assignments + `is_new` flags + importance roll-ups). See [`../tags/readme_tags.md`](../tags/readme_tags.md) for outputs and the design / class spec.

Set `TAGS_ENABLED = False` near the top of `run_linking.py` to bypass the tagging pipeline and reproduce the original linker-only behaviour (same OUTPUT shape).

The script loads the extracted JSON (with a robust record-boundary fallback for malformed files), parses every record through its supertype schema, runs the linker, and writes the result as a JSON dict with an `events` list. It prints counts of input records, linked events, drop reasons, and how many events were merged from multiple sources. Set `GEOCODE = False` at the top to skip geocoding (events with no resolvable state will fall into the empty-prefix bucket).

### Required environment

The linker reads `OPENROUTER_API_KEY` (loaded from `kg/.env.local` automatically by `run_linking.py`) and the deepriver geocoder microservice URLs (`NLP_URL`, `GEOCODING_URL`) — set the latter in your shell or local `.env`. Override the linker model via `OPENROUTER_LINKER_MODEL` (default `google/gemini-2.5-flash-lite`).

## KG Database Persistence

> **Status: not yet implemented.** The linker does **not** currently write anything to a database — its output is the in-memory / JSON record described in [Output record shape](#output-record-shape). The model in this section is the **target** persistence design for the **kgdb** Postgres database (the unified entities knowledge graph). It exists to guide code structure and architecture decisions while we iterate on different linking approaches; once linking stabilises, the pipeline will start writing to kgdb following this contract. The full schema and cross-database conventions are documented in [`media-backend-paid/docs/DATABASE_POSTGRES.md`](../../../../../media-backend-paid/docs/DATABASE_POSTGRES.md) — this section captures the pieces relevant to the linker's eventual write path.

### Persistence model — overview

The whole KG ontology is encoded in the existing kgdb tables — no new tables are introduced.

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

The KG ontology has three top-level categories — **event**, **entity**, **theme** — declared on every supertype schema as `meta.category`. They map 1:1 to rows in `entity_kinds_available`. Currently the table contains `event` and `entity`; **`theme` will be added when theme rows start being written**.

### Supertypes and types (`entity_types_kinds_available`)

The KG ontology is two-level: a **supertype** (e.g. `paid_mass_event`, `legislative_initiative`, `security`) defines the JSON schema, and one or more **child types** (e.g. `concert`, `festival`; `law_initiative`, `decree`; `crime_trends`, `law_enforcement`) refine it. Both live as rows in `entity_types_kinds_available`, distinguished by `parent_entity_type`:

| Row | `entity_kind` | `parent_entity_type` | `metadata_template` |
|---|---|---|---|
| Supertype | `event` / `entity` / `theme` | `NULL` | The full JSON schema (= the contents of `../extraction/schemas/{supertype}.json`) |
| Child type | same as parent's | The supertype's `entity_type_id` | `NULL` (inherits the parent's schema) |

**Inheritance scope** is intentionally limited to supertype → child type. Children do not currently override or extend the supertype schema — extracted records for any child of `paid_mass_event` are validated against `paid_mass_event.json` regardless of which child class produced them. Storing the schema only on the supertype keeps a single source of truth and avoids fan-out updates across every child row when a schema evolves. If/when child-level field overrides are needed, the child row's `metadata_template` will carry the delta (extra fields), and validation will need to merge parent + child schemas before parsing.

### Canonical records (`entities`, `entity_types`)

Every linked output of the pipeline becomes one row in `entities`:

- `name`, `description`, `keywords`, `embedding`, `filter_llm_prompt` — populated from the linked record (or left blank for now where the pipeline doesn't produce them).
- `metadata` (`json`) — the validated, schema-conformant extracted record (output of the schema `Parser` for the supertype). Shape depends on the supertype:
  - `paid_mass_event`: `event_type`, `date_range`, `location`, `price_range`, `attendance`, …
  - `legislative_initiative`: `entity_type`, `name`, `jurisdiction`, `date_introduced`, `legislative_body`, …
  - theme supertypes: `theme_type`, optional `location`, plus per-article fields (see *Themes* below)

The entity is associated with its supertype (and, when known, the child type) via `entity_types` rows pointing at `entity_types_kinds_available.entity_type_id`. `entity_types.entity_id` references `entities_alias.original_entity_id`, so entity merges remain stable across the indirection layer.

> **Future: multi-class entities.** Today the linker writes one supertype (+ optional child type) per entity, but `entity_types` is already a many-to-many — a single `entity_id` can carry multiple `entity_type_id` rows. An entity instantiating more than one ontology class simultaneously (e.g. an event that's both a `paid_mass_event` and a `protest_event`, or a `legislative_initiative` that also acts as a `security` theme anchor) is a real possibility we'll address when inheritance work properly lands. The schema accommodates it; the open questions are at the validation layer (which class's schema does `entities.metadata` conform to?) and at the linker (does multi-class change the candidate filter?). Until inheritance is tackled, treat one supertype per entity as the working assumption.

### Themes are degenerate single entities

A theme is a topical classifier — every article matching `(theme_class, location_up_to_level_3)` should link to the **same** `entities` row. The KG never produces a unique "instance" of a theme; instead, the linker maintains one canonical theme entity per `(theme_supertype_or_child_type, level_1, level_2, level_3)` tuple and appends `entities_documents` rows for each matching article.

Consequently, the theme schema's article-side fields (`description`, `tags`, `context`, `relevance`, `sentiment`, `related_subtopics`, `time_scope`) describe a particular article's take on the theme, not a stable property of the canonical theme entity. **Recommendation:** for theme rows, keep `entities.metadata` minimal — only `theme_type`/`theme_subtype` and the location reference. Per-article variations belong on the `entities_documents` link, not on the canonical entity. (Per-article sentiment already has a home in `entities_documents_sentiments`.)

### Locations (`entity_locations`)

Events, and some entities, carry one or more rows in `entity_locations`. The `entity_locations` schema is intentionally aligned with the deepriver geocoder output (see [`geocode.py`](geocode.py)):

| `entity_locations` column | Geocoder field |
|---|---|
| `coords` (`point`) | `(matched_lon, matched_lat)` |
| `formatted_name` | `formatted_name` |
| `precision_level` | `precision_level` (1–7) |
| `geoid` | `geoid` |
| `level_{N}` / `level_{N}_id` | `level_N` for N=1..7 (1=country, 2=state, 3=city, 5=neighborhood, 6=street, 7=place) |

Multiple locations per entity are allowed (one row per location). Themes are the only category whose canonical-entity identity *requires* a coarse location (up to level 3) — see *Themes are degenerate single entities* above.

### Linking lookups (`event_properties`)

`event_properties` is the index that the linker uses to find candidate matches for a new incoming event without parsing every `entities.metadata` JSON blob. It carries the fields the candidate filter needs:

| Filter dimension | Source for an incoming event | Stored on the linked event row |
|---|---|---|
| Date overlap | `metadata.date_range.date_range.{start,end}` | `event_properties.date_start`, `event_properties.date_end` |
| Geographic prefix | geocoded `level_2_id` of the event's location | `entity_locations.level_2_id` |
| Type compatibility | `metadata.event_type` (resolved to `entity_type_id`) | `entity_types.entity_type_id` |

Without `event_properties`, the linker would have to extract `date_start`/`date_end` from each candidate's `metadata` JSON at query time — a JSON-path scan over all events of the right type and area. Materialising them as columns lets a normal range index drive candidate retrieval.

`status` and `status_date` track event lifecycle (e.g. an `arrest_event` moving from `reported` → `confirmed` → `closed`) without rewriting `metadata` — useful when only the lifecycle changed but the underlying extracted record is unchanged.

#### Open question: fold `event_properties` into `entities`?

The fields are small (3 timestamps, 1 status string) and tightly coupled to event rows, so folding them in would save a join.

**Recommendation: keep `event_properties` separate.** Three reasons:

1. `entities` is shared across all three categories. Event-only columns on it would be `NULL` for ~⅔ of rows (themes and entities/concepts) and grow as new event-only or entity-only properties emerge — the table heads toward a sparse heterogeneous schema. The same pattern (typed property tables alongside `entities`) generalises to future categories — e.g. an analogous property table for legislative initiatives carrying `date_introduced`, `voting_status`, etc.
2. Status updates are far more frequent than full-record rewrites. Keeping them off `entities` means status churn doesn't dirty the canonical row (and its embedding/keywords/metadata) on every state transition, and doesn't compete for autovacuum on a much larger table.
3. The linker's join cost is bounded by `(entity_type_id, level_2_id, date overlap)`, all highly selective; a covering index on `event_properties (date_start, date_end)` plus the existing `event_id` constraint keeps the candidate fetch cheap.

If linking latency ever becomes the bottleneck, the cheaper move is to add the index above, not to denormalise.

### Pipeline write path (sketch — not yet wired up)

The steps below describe the eventual write path. None of them are implemented today; the linker stops at the in-memory linked record. For each linked record produced by the linker, the persistence layer (when added) will:

1. Resolve or create the entity:
   - For events/entities: insert a new `entities` row (with `metadata` = the validated record), then an `entities_alias` row with `original_entity_id = current_entity_id = entities.entity_id`.
   - For themes: look up the canonical `(theme_type, level_1, level_2, level_3)` entity and reuse its `entity_id`, or create one if absent.
2. Insert `entity_types` rows linking the entity (via `entities_alias.original_entity_id`) to the supertype's `entity_type_id` and, when known, the child type's.
3. For each geocoded location, insert an `entity_locations` row with the geocoder's level breakdown.
4. For events, insert/update the `event_properties` row with `date_start`, `date_end`, `status`.
5. For each source document that mentions the linked record, upsert `entities_documents (entity_id, doc_id)` to register the link (org-agnostic, per the existing sentiment write path).

`entity_id` in step 5 (and in any user-facing references) is always `entities_alias.original_entity_id`, so later entity merges don't break the link.

### Direct-FK exception (recap)

`event_properties.event_id`, `entity_locations.entity_id`, and `relations.ent_id_*` FK directly to `entities.entity_id` rather than `entities_alias.original_entity_id` (a known oversight pending migration). Until that migration lands, the pipeline must take care to write to the surviving `entities.entity_id` (resolve the alias indirection before insert) and entity-merge logic must rewrite these rows. See the *Cross-database references* and *Exceptions* sections of [`DATABASE_POSTGRES.md`](../../../../../media-backend-paid/docs/DATABASE_POSTGRES.md) for the canonical write.
