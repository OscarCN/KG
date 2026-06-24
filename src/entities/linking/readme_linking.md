# Entities — Linking

Deduplicates and merges extracted **events** (the output of `../extraction/`) into canonical event records, each carrying a `source_ids` list of every document that mentions it. Future versions will link entities/concepts and themes too — see [KG Database Persistence](#kg-database-persistence) below for the persistence model that already accounts for them.

For an overview of the broader pipeline and ontology categories, see [`../readme_entities.md`](../readme_entities.md).

## Directory Structure

```
linking/
  geocode.py              # Geocoder wrapper (structured Location → full level_1..7 + level_N_id + coords + geoid)
  geo_util.py             # Coords-only helpers: haversine (meters), grid_cell/grid_neighbors (~330m retrieval buckets)
  text_util.py            # name_similarity (character-trigram Jaccard, accent-insensitive) — name strings only
  link_llm.py             # LLM disambiguator (gemini-2.5-flash-lite) with file cache
  index.py                # CandidateIndex + RecordStore protocols + in-memory implementations
  kgdb_retrieval.py       # kgdb-backed CandidateIndex (SQL column-reconstruction) + RecordStore (reads entities.metadata)
  mx_states.py            # Static catalogue of the 32 Mexican states (partition-key normalization + fallback)
  strategy.py             # GeoEventStrategy: enrich → window/keys → deterministic gate / adjudicate → merge/create
  link.py                 # EntityLinker: envelope parse + strategy orchestration + case log. link_one(raw) → LinkResult.
  persistence.py          # KgdbWriter: idempotent write of a linked record into kgdb (Step Zero batch/stream writer)
  run_linking.py          # IPython runner — tests linking from extracted-record fixtures (env-configurable stem).
  readme_linking.md       # This file
```

The runner streams extracted records grouped by `_source_id` and invokes `EntityLinker.link_one(raw)` per record. It is only a local test harness for the linking system after extraction; it does not fetch article/comment content and does not run tags.

For a document-level stream simulation that runs extraction before linking each incoming document, use [`../run_entities.py`](../run_entities.py). That script composes `EntityExtractor.extract(article)` with this linker's `EntityLinker.link_one(raw)` streaming API.

## Linking Pipeline

**Scope: events only.** Records whose schema `meta.category != "event"` (themes, entities/concepts) are skipped by this version of the linker — they're tallied under `linker.dropped["skipped_category:..."]` and can be revisited later.

The geo strategy v1 spec in [`docs/todos/retrieval_linking_per_supertype.md`](../../../docs/todos/retrieval_linking_per_supertype.md) is **implemented**; that TODO also tracks the per-supertype generalization (still open).

```
new event → schema parse (EntityLinker envelope)
          → strategy.prepare: geocode → geo partition keys + date window
          → candidate lookup (CandidateIndex: supertype ∧ geo-key ∧ day-key overlap)
          → hard geo gate (drop candidates whose location isn't hierarchically contained either way)
          → deterministic gate (shares level_6/7_id ∧ day → skip the LLM, no name)
          → else LLM adjudication (gemini-2.5-flash-lite, capped candidate list)
          → match-id ? merge : create new
```

**Architecture.** `EntityLinker` (`link.py`) is supertype-agnostic: it parses the record envelope (`_source_id`, `_supertype`, `date_created`), selects a strategy by the schema's `meta.category`, and orchestrates the calls. All event-specific behaviour lives in `GeoEventStrategy` (`strategy.py`), which owns the full lifecycle — enrich → window/key construction → adjudication → merge/create → (re)index — against the `CandidateIndex` protocol (`index.py`): the in-memory pair for batch/test runs, the kgdb-backed `KgdbCandidateIndex`/`KgdbRecordStore` (`kgdb_retrieval.py`) for the streaming consumer. A supertype with no schema is a logged drop (`no_schema`) — it no longer silently defaults to the event path.

**Identity model (geo events).** Two records denote the same event iff: same **supertype** (soft type — the leaf `event_type` may differ between sibling classes; `partition_on="event_type"` for the legacy exact-type rule), **geo-compatible** place (one location's admin id-path hierarchically contained in the other's — a *hard* gate under `hard_geo_gate=True`, never overruled by the LLM), overlapping time (tiered fallbacks below), and co-referent content (the deterministic level-6/7-share gate, or the LLM over the description).

### Candidate filter

For each incoming event, candidates are the already-linked events sharing **all three** of:

- same **type partition** — by default the **supertype** (`partition_on="supertype"`, *soft type*): candidates span sibling leaf `event_type`s (e.g. a `concert` and a `festival` of the same `paid_mass_event` supertype can be the same event), and the leaf-type decision is left to the deterministic gate / LLM. `partition_on="event_type"` reproduces the legacy *hard type* partition (one bucket per leaf type).
- overlapping **geo partition** — hierarchical (`geo_retrieval="hierarchy"`, the default; legacy `"level_2"` reproduces the old single state slug). A **located** record registers and looks up under its *fine* keys only — each available `level_N_id` below state (`partition_levels=(3,5,6,7)`) **and** a coordinate **grid cell** (`grid_size_deg≈0.003`, ~330 m). Lookup additionally probes the grid cell's **8 neighbors** (a same-event mention can land in an adjacent cell), so the grid's total retrieval reach is the ~1 km-wide 3×3 block. This is deliberately *not* a shared state-wide bucket — that would re-merge every located event in the state (the single-state degeneracy). Cross-municipality recall is instead carried by the grid: two mentions of the same place that disagree on `level_3_id` (e.g. a sinkhole tagged Corregidora vs Querétaro-city) still meet in the same/adjacent cell. A record with no fine keys falls back to a **state-only** bucket (`so:<slug>`, from the geocoder `level_2` name or the extracted `location.state` via the `mx_states.py` catalogue) or, with no state at all, the **noloc** bucket (`""`). `_geo_source ∈ {geocoder, state_catalogue, none}` records which tier produced the state slug. Located lookups also probe the `so:`/noloc buckets as a one-way **bridge** so a precise mention can match an earlier vague one (the reverse is impossible — a vague record can't know the partition; accepted recall trade-off).
- date-range overlap with **slack** applied symmetrically:
  - **max(±1 day, ±`precision_days`)** when the incoming event has an extracted `date_range` — an approximate mention ("en marzo" → `precision_days≈30`) widens its own window accordingly.
  - **±2 days** when the incoming event has no extracted date and falls back to its publication timestamp. (Kept symmetric deliberately: publication can precede or follow the event for most types — announcements vs. reports.)

Each linked event is registered in the candidate index under both its extracted-date window (when present, with its own slack) and its publication-date window (when present, with publication slack). That way the next incoming event finds it regardless of which date source it carries. Windows longer than `max_window_days` (365) are clamped at the start + 365 days and logged.

The filter is intentionally broad (recall) — the **deterministic gate or the LLM** makes the actual same-vs-different judgment (precision), over a candidate list capped at `candidate_cap` (12, most recent first).

#### Hard geo gate

With `hard_geo_gate=True` (default), **geo is a hard candidate gate, not just a partition**: after retrieval, candidates whose location is not *hierarchically compatible* with the incoming event are dropped before the deterministic gate and the LLM ever see them. Two locations are compatible iff one's admin id-path (`level_1_id…level_7_id`, level 4 unused) is **contained in** the other's — the coarser location is a strict prefix of the finer one (e.g. `level_3` Querétaro-city ⊂ a `level_6` street within it). Different ids at any shared level (Mérida vs Toluca, two distinct streets in the same colonia) ⇒ incompatible, so the LLM can never overrule geo to merge them. A record with **no admin id-path at all** (noloc) is incompatible with everything: an unknown location can't be *confirmed* to match, which stops a location-less record from becoming a name magnet that swallows every same-named event across the country. This is what "hard geo" buys over the soft partition alone — the grid/neighbor probes and the `so:`/noloc bridge still widen *retrieval*, but the gate guarantees a merge never crosses an incompatible location. `hard_geo_gate=False` restores the legacy behaviour where geo only partitions and the LLM may merge across partitions it was shown.

> **History.** v1 partitioned on a single state slug (after fixing a bug where it keyed on `_geo["level_2_id"]`, which the geocoder wrapper never emitted, so the partition never fired and all same-type/same-day events shared one bucket). That single-state partition is **superseded** by the hierarchical `level_N_id` + grid retrieval above (`geo_retrieval="hierarchy"`; `"level_2"` reproduces the v1 single-slug behaviour for regression). One observed geocoder quirk persists: the wrapper picks the highest-`precision_level` match regardless of state agreement, so a record whose extracted `state` says one state can land in another's partition; this is deterministic (cached), so partitioning stays consistent per location input.

#### Strategy parameters

`GeoEventStrategy` exposes every behaviour above as a constructor parameter (`EntityLinker(strategy_params={...})`), including legacy values that reproduce the pre-refactor behaviour exactly for regression runs:

| Parameter | Default | Legacy value |
|---|---|---|
| `geo_partition_field` | `"level_2"` | `"level_2_id"` (the bug — always `""`) |
| `state_catalogue_fallback` | `True` | `False` |
| `probe_noloc_bucket` | `True` | n/a (legacy had one bucket) |
| `precision_aware_slack` | `True` | `False` (fixed ±1) |
| `max_window_days` / `clamp_long_ranges` | `365` / `True` | `False` (endpoints-only quirk) |
| `bounded_merge_widening` | `True` | `False` (unconditional min/max) |
| `candidate_cap` | `12` | `None` (unbounded) |
| `geo_retrieval` | `"hierarchy"` | `"level_2"` (single state slug) |
| `partition_on` | `"supertype"` (soft type — one partition per supertype, candidates span sibling leaf `event_type`s) | `"event_type"` (hard type — one partition per leaf type) |
| `partition_levels` | `(3, 5, 6, 7)` | n/a (state only) |
| `grid_size_deg` | `0.003` (~330 m cells → ~1 km across the 3×3 lookup block) | n/a (no grid) |
| `hard_geo_gate` | `True` (geo is a hard candidate gate — hierarchical containment) | `False` (geo only partitions; the LLM may overrule geo) |
| `deterministic_merge` | `True` | `False` (LLM-always) |
| `deterministic_share_levels` | `(6, 7)` | — |
| `det_day_slack` | `1` | — |
| `det_publication_levels` | `(7,)` (publication-date merges allowed at the leaf) | — |

### Date sources

Each extracted record may carry two date provenance fields:

| Field | Source | Used when |
|---|---|---|
| `date_range.date_range.{start,end}` | LLM-extracted from the article body | The article explicitly mentions when the event happened |
| `date_created` | Article publication timestamp, attached by `extract.py` (and copyable from ES via `src/PoC/enrich_extracted_raw.py`) | Falls back to this when no date is extracted |

Records with neither are dropped (`linker.dropped["event_no_date_no_pub"]`). When both are present, the extracted date_range wins for candidate-window resolution; the publication date is still kept for index registration so future publication-only records can find this event.

The linked record carries `publication_date` (the **earliest** publication date seen across merged sources) — useful as a stable temporal anchor when the extracted date_range is missing or imprecise — plus `_source_windows`, the list of every source's resolved window (`{start, end, slack_days, source, precision_days}`), and `_date_source` (the first window's provenance).

### Deterministic merge gate (skip the LLM)

Before the LLM, a cheap deterministic pass merges the high-confidence cases (`deterministic_merge=True`). It is **name-agnostic and precision-aware**: an incoming event merges into a candidate without an LLM call when they

- share a **`level_6_id` or `level_7_id`** — same street or same place (`deterministic_share_levels=(6,7)`), **and**
- have **overlapping dates** (within `det_day_slack`, 1 day): an **extracted** date on both sides, *or* — when the shared id is at a leaf level in `det_publication_levels` (level 7 / place by default) — a **publication-date** overlap is accepted too. One specific place hosts ~one event of a type per day, so a publication-day match there is safe; this is what lets nameless incident reports (which usually carry only a publication timestamp) merge. Widen `det_publication_levels` to `(6, 7)` to allow it at street level too (riskier — amplifies the same-street weakness below).

`event_type` is already guaranteed by the partition, and **no name is required**. Sharing a fine id can only happen if *both* records reached street/place precision, so the rule is precision-aware by construction: a coarse record (no `level_6/7_id` — e.g. one geocoded only to the municipality) has nothing to share and always defers to the LLM. A hit is logged with `path="deterministic"`. The gate never decides on coordinate *distance* — an earlier distance-based version could merge a municipality-centroid record into a precise one when the centroid happened to fall within radius; keying on shared fine ids removes that. (The case log still reports each candidate's `geo_dist_m`/`name_sim` for audit, even though the decision no longer uses them.)

#### Accepted weaknesses

- **Same-street collisions (level 6).** Two *distinct* same-type events on the same street and day (e.g. two separate accidents) share `level_6_id` and will merge — accepted for now in exchange for catching the common multi-mention case. Level 7 (a specific place) is safer than level 6 (a whole street).
- **Coarse-precision under-merge (levels 2/3/5).** Records that geocode only to municipality / city / neighborhood share no `level_6/7_id`, so nameless coarse clusters are never matched deterministically and rely on the LLM (which may under-merge). **Canonical example:** the El Marqués street-rehabilitation project on *calles San Juan del Río y Amealco* — every record nameless, all geocoded to `level_3_id=_48422011` (precision 3) — stays fragmented. The fix is list-valued location extraction so each named street geocodes to level 6: [`docs/todos/location_level_list_extraction.md`](../../../docs/todos/location_level_list_extraction.md).

### LLM disambiguation (`link_llm.py`)

When the deterministic gate does not fire, a single LLM call decides whether the incoming event matches any candidate. The model is `google/gemini-2.5-flash-lite` (override via `OPENROUTER_LINKER_MODEL`). The payload is **description-centric**: identity is judged on the *described facts*, not a privileged name (most records have none). For the incoming event and each candidate it contains ONLY:

| Field | Source |
|---|---|
| `identification` | record `description`, **description-led**, with the `name` folded in only when present and not already in the text — never a standalone key |
| `address` | the structured `location` dict (country, state, city, neighborhood, zone, street, number, place_name) — **not** the whole event record |
| `date` | `{start, end}` from `record.date_range.date_range`, ISO-formatted (may both be null) |
| `publication_date` | ISO-formatted publication timestamp (when present); used by the LLM as a fallback temporal anchor |
| `ubicacion_fina` *(candidates only)* | `misma` / `distinta` / `null` — whether the candidate resolves to the same fine (street/place) location as the incoming event (from `level_6/7_id`); a negative signal when `distinta`. The prompt tells the model to treat `distinta` as different events absent strong evidence. |

The system prompt instructs the model to merge complementary/partial descriptions of the same concrete event and to keep genuinely different facts (different works, incidents, or specific places) apart — explicitly *not* relying on the name, and to treat a `ubicacion_fina="distinta"` candidate as a different event absent strong evidence.

> **Note (soft signal vs hard gate):** `ubicacion_fina` is advisory — the LLM can overrule it, and on the public_works fixture it did **not** prevent the Paseo de México ↔ Paseo de Belgrado sinkhole over-merge (two distinct streets, differing `level_6_id`s in colonia Tejeda). The [hard geo gate](#hard-geo-gate) (`hard_geo_gate=True`, default) now prevents exactly this: those candidates contradict at `level_6_id`, so they're dropped before the LLM is ever asked. The residual risk moves to geocoder leaf accuracy — if two mentions of the *same* place get *different* leaf ids, the gate keeps them apart with no LLM fallback; measure per-level leaf consistency and soften the gate at noisy levels if needed.

Candidates additionally carry their `id`. The LLM is instructed to return either `{"match_id": "<one of the candidate ids>"}` or `{"match_id": null}`. Any id not present in the candidate list is treated defensively as `null`. Empty candidate lists short-circuit to `null` without an LLM call.

Responses are cached as `cache/link_llm/<sha256>.json`, keyed by `sha256(canonical(payload))`. Re-runs hit the cache and produce identical decisions without re-billing.

### Merge behavior

When a match is found — by the deterministic gate or the LLM — the strategy:

- appends the new record's `_source_id` to the canonical event's `source_ids` (de-duped),
- fills nulls on `name`, `description`, `context`, `status` from the new record; keeps the earliest `publication_date`,
- appends the incoming window to `_source_windows` and sets the canonical `date_range` to the **most precise extracted window seen** (smallest `precision_days`, `None` = exact; ties keep the earliest-seen) — the canonical range no longer widens unconditionally, which prevented one imprecise source from permanently inflating the window and snowballing future merges. Registration stays generous: the incoming window's day-keys are registered regardless, so recall is unchanged,
- promotes the canonical record's `location` to the new one by **precision** under the hard geo gate (`hard_geo_gate=True`): a merge only joins geo-compatible records, so a higher-`precision_level` incoming location refines the canonical (`location`, `_geo`, `_geo_source`), and the re-registration step re-indexes the event under the finer geo keys — a coarse seed can't stay a magnet. With the gate off, it falls back to the legacy rule (promote when the new `location` has more populated subfields **and** resolves to the same geo partition).

When no match is found, a new linked event is minted with id `{YYYYMMDD}_{state-slug-or-noloc}_{rand}`.

### Drop reasons

| Bucket | Why |
|---|---|
| `skipped_category:<theme\|entity\|...>` | Record's schema is not an event — no strategy registered for its category |
| `no_schema` | Record's `_supertype` has no schema file — previously these silently defaulted to the event path unparsed; now an explicit, logged drop |
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
| `neighborhood` | `COL` | 5 |
| `street` (+ `number`) | `CALLE` | 6 |
| `place_name` | `LUG` | 7 |

`zone` is **not** geocoded. Per the `Location` schema it is a generic directional/functional area with no residential proper name ("zona norte", "corredor industrial"), distinct from `neighborhood` (a named colonia). Sent as `COL` it mis-matched a literal colonia of that name in another state — e.g. `zone="corredor industrial"` dragged *caseta de cobro Palmillas* to a Tamaulipas colonia (precision 5), and `zone="sur"` dragged *Riviera Maya* to colonia SUR, Sonora. Dropping it removed those cross-state mismatches (and let Palmillas resolve correctly to its level-7 caseta in Querétaro). `zone` is still kept on the extracted record, just not used for geocoding.

The geocoder is deepriver's own NLP + geocoding microservice pair, reached via `NLP_URL` and `GEOCODING_URL` env vars. The wrapper picks the highest-precision match from context group `'1'` of the response and exposes `geoid`, `precision_level` (int 1–7), `formatted_name`, the full admin hierarchy as both names (`level_1`…`level_7`) and hierarchical ids (`level_1_id`…`level_7_id`), and `matched_lat`/`matched_lon`. The `level_N_id`s nest as strict prefixes (`_484` ⊂ `_48422` ⊂ `_48422016`), mirror kgdb `entity_locations.level_N_id`, and are what the geo partition keys are built from. Results are cached as JSON under `cache/geocode/<sha256>.json` keyed by the canonicalized Location dict, so re-runs avoid hitting the geocoding service — note the cache stores the normalized output, so changing which fields the wrapper retains requires clearing `cache/geocode/` to repopulate.

### Output record shape

Each linked event extends the original schema with these link-level fields:

| Field | Type | Description |
|---|---|---|
| `id` | str | Minted linked id (`{YYYYMMDD}_{state-slug-or-noloc}_{rand}`) |
| `source_ids` | List[str] | `_source_id`s of every document that mentions this event |
| `publication_date` | str | Earliest publication timestamp across the merged sources (when any source had one) |
| `_date_source` | str | Provenance of the first source's window (`extracted` / `publication`) |
| `_source_windows` | List[dict] | Every source's resolved window: `{start, end, slack_days, source, precision_days}` |
| `_sources` | List[dict] | Per-source document metadata, de-duped by `source_id`: `{source_id, publication_date, news_type}` — each source's OWN publication date and `news_type` (carried article → record → `_sources`), so `entities_documents` can be written per source rather than stamping the canonical earliest date on every document |
| `_geo_source` | str | Which tier produced the geo partition key (`geocoder` / `state_catalogue` / `none`) |

Linked events also carry the canonical `date_range` (the most precise extracted window seen across sources), the most-populated `location` fields seen across sources within the same geo partition, and the `_geo` block from the geocoder.

### Running

`run_linking.py` is a step-by-step IPython script (mirrors `src/PoC/run_extraction.py`) for testing linking against an extracted-record fixture. Select the fixture with `LINK_STEM` (or `LINK_INPUT_STEM`/`LINK_OUTPUT_STEM`) and point the geocoder via `GEOCODING_URL`/`NLP_URL`; `GEOCODE` (env or the constant) toggles geocoding:

```bash
GEOCODING_URL=http://localhost:8090/geocoder NLP_URL=http://localhost:8210/tag \
LINK_STEM=geo_qro_public_works_event \
ipython src/entities/linking/run_linking.py
# or from a Jupyter/IPython session:
%run src/entities/linking/run_linking.py
```

After it finishes, the following names are bound for inspection:

| Name | What it holds |
|---|---|
| `records` | Raw extracted records loaded from `INPUT` |
| `records_by_source` | Raw records grouped by `_source_id` |
| `source_ids_in_order` | Source ids processed in publication-date order |
| `linker` | The `EntityLinker` instance (with `linker.dropped`, `linker.events`, ...) |
| `link_results` | One `LinkResult` per input record |
| `linked` | Dict with an `events` list (themes/entities are skipped) |

The script loads the extracted JSON fixture, streams every record through the linker, and writes the result as a JSON dict with an `events` list. It prints counts of input records, link-result statuses, linked events, drop reasons, and how many events were merged from multiple sources. Set `GEOCODE = False` at the top to skip geocoding (events with no resolvable state will fall into the empty-prefix bucket). Set `LINK_STEM` (or `LINK_INPUT_STEM`/`LINK_OUTPUT_STEM`) to drive a different fixture without editing the file.

### Case log

`EntityLinker(case_log_path=...)` writes one JSONL line per linked record (`run_linking.py` points it at `data/.runlogs/linking_cases_<output-stem>.jsonl`). Each line carries the record's `geo` (coords, `level_3_id`, `_geo_source`), `window`, the retrieved `candidates` (each with `geo_dist_m` and `name_sim`), the decision `path` (`no_candidates` / `deterministic` / `llm`) and the `decision` (`created:<id>` / `merged:<id>`). It is the audit trail for the deterministic gate and the input to tuning thresholds — e.g. the public_works run above showed the nameless-record gap directly.

### Required environment

The linker reads `OPENROUTER_API_KEY` (loaded from `kg/.env.local` automatically by `run_linking.py`) and the deepriver geocoder microservice URLs (`NLP_URL`, `GEOCODING_URL`) — set the latter in your shell or local `.env`. Override the linker model via `OPENROUTER_LINKER_MODEL` (default `google/gemini-2.5-flash-lite`).

## KG Database Persistence

> **Status: streaming + kgdb-backed retrieval implemented (validated on dev).** [`persistence.py`](persistence.py) (`KgdbWriter`) writes linked records into the **kgdb** Postgres database following the model below — in batch via [`scripts/persist_linked.py`](../../../scripts/persist_linked.py) from a `data/linked/<stem>.json` fixture (validated on dev: the `geo_qro_paid_mass_event` fixture — 463 entities + 926 `entity_types` + 463 `event_properties` + 953 `entities_documents`, idempotent re-runs) and inline per message by the **streaming RabbitMQ consumer** [`src/listener.py`](../../listener.py) (consume documents → extract → link → `KgdbWriter.upsert_linked`). The consumer uses kgdb-backed candidate retrieval ([`kgdb_retrieval.py`](kgdb_retrieval.py): `KgdbCandidateIndex`/`KgdbRecordStore`), so dedup holds across restarts and workers (validated on dev over multi-hundred-document batches). Still pending: the **in-DB canonical↔canonical merge** (reconciliation) and the production **producer/retriever** — see [`docs/todos/kgdb_event_persistence.md`](../../../docs/todos/kgdb_event_persistence.md). Residual risk: candidate-lookup→adjudicate→create runs in the linker outside any DB lock, so under true multi-worker parallelism duplicate canonicals are still possible — covered by [`docs/todos/canonical_reconciliation.md`](../../../docs/todos/canonical_reconciliation.md). The full schema and cross-database conventions are documented in [`media-backend-paid/docs/DATABASE_POSTGRES.md`](../../../../../media-backend-paid/docs/DATABASE_POSTGRES.md).

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

The kgdb candidate-retrieval predicates ([`kgdb_retrieval.py`](kgdb_retrieval.py)) are now backed by indexes in the schema (`media-backend-paid/db/kg_db/schema.sql`, asserted by [`test_kgdb_indexes.py`](test_kgdb_indexes.py)): expression indexes on `entities (metadata->>'_link_id')` and `(metadata->>'_supertype')`, a GiST on the `event_properties` date range, btrees on `entity_locations.level_N_id`, and a GiST on coords. So the date/geo/type/`_link_id` lookups don't scan. Denormalising `event_properties` into `entities` remains unnecessary.

### Pipeline write path (`KgdbWriter.write_linked`)

`KgdbWriter` ([`persistence.py`](persistence.py)) implements the steps below — one transaction per linked record (the unit the future streaming consumer calls per message). Themes are not linked upstream, so the theme branch in step 1 is not exercised yet. For each linked record:

1. Resolve or create the entity:
   - For events/entities: insert a new `entities` row (`metadata` = the validated record **+** `_link_id`/`_link_run` provenance), then an `entities_alias` row with `original_entity_id = current_entity_id = entities.entity_id`.
   - *(Planned)* For themes: look up the canonical `(theme_type, level_1, level_2, level_3)` entity and reuse its `entity_id`, or create one if absent.
2. Insert `entity_types` rows linking the entity (via `entities_alias.original_entity_id`) to the supertype's `entity_type_id` and, when the leaf resolves, the child type's.
3. For the geocoded location (`record["_geo"]`), insert an `entity_locations` row with the geocoder's level breakdown (skipped when no `_geo`). One row today; multi-row once [list locations](../../../docs/todos/location_level_list_extraction.md) land.
4. For events, upsert the `event_properties` row (`ON CONFLICT (event_id)`) with the **slack-widened** `date_start`/`date_end` (so a `tstzrange &&` index reproduces the candidate date filter) and `status`.
5. For each `_sources` entry, upsert `entities_documents (entity_id, doc_id)` (`doc_index='news'`) carrying that source's OWN `doc_date_created` (its `publication_date`) and `news_type` — not the canonical earliest date — with `doc_source`=host, org-agnostic, per the existing sentiment write path. (Old records without `_sources` fall back to `source_ids` + the canonical date.)

`entity_id` everywhere is `entities_alias.original_entity_id` (== `entity_id` at create), so later entity merges don't break the link.

**Idempotency.** The batch path (`write_linked`) is run-scoped: records already written under the run (`metadata->>'_link_id'` **+** `_link_run`) are skipped, and `KgdbWriter.reset_run()` deletes a run (child→parent order) for a clean re-write. The streaming path (`upsert_linked`) matches an existing canonical by `metadata->>'_link_id'` **alone** (run-tag-independent), so a new run or backfill merges into an existing canonical instead of duplicating it.

**Concurrent merges are additive.** The in-place update locks the canonical row (`SELECT … FOR UPDATE`) and UNIONs the DB's `source_ids` / `_source_windows` / `_sources` accumulators with the incoming record before writing, so two workers merging different sources into the same canonical don't clobber each other's source accumulators (no last-writer-wins loss).

Drop buckets: `no_supertype`, `unseeded_supertype:<name>`, `error`.

### Direct-FK exception (recap)

`event_properties.event_id`, `entity_locations.entity_id`, and `relations.ent_id_*` FK directly to `entities.entity_id` rather than `entities_alias.original_entity_id` (a known oversight pending migration). Until that migration lands, the pipeline must take care to write to the surviving `entities.entity_id` (resolve the alias indirection before insert) and entity-merge logic must rewrite these rows. See the *Cross-database references* and *Exceptions* sections of [`DATABASE_POSTGRES.md`](../../../../../media-backend-paid/docs/DATABASE_POSTGRES.md) for the canonical write.
