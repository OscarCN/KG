# Entities — Linking

Deduplicates and merges extracted **events** (the output of [extraction.md](extraction.md)) into canonical event records, each carrying a `source_ids` list of every document that mentions it. Future versions will link entities/concepts and themes too — see [storage.md](storage.md) for the persistence model that already accounts for them.

For an overview of the broader pipeline and ontology categories, see [entities.md](entities.md).

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
```

This file lives in `docs/linking.md`; the source it documents is under `../src/entities/linking/`.

The runner streams extracted records grouped by `_source_id` and invokes `EntityLinker.link_one(raw)` per record. It is only a local test harness for the linking system after extraction; it does not fetch article/comment content and does not run tags.

For a document-level stream simulation that runs extraction before linking each incoming document, use [`../src/entities/run_entities.py`](../src/entities/run_entities.py). That script composes `EntityExtractor.extract(article)` with this linker's `EntityLinker.link_one(raw)` streaming API.

## Linking Pipeline

**Scope: events only.** Records whose schema `meta.category != "event"` (themes, entities/concepts) are skipped by this version of the linker — they're tallied under `linker.dropped["skipped_category:..."]` and can be revisited later.

The geo strategy v1 spec in [`todos/retrieval_linking_per_supertype.md`](todos/retrieval_linking_per_supertype.md) is **implemented**; that TODO also tracks the per-supertype generalization (still open).

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
- **Coarse-precision under-merge (levels 2/3/5).** Records that geocode only to municipality / city / neighborhood share no `level_6/7_id`, so nameless coarse clusters are never matched deterministically and rely on the LLM (which may under-merge). **Canonical example:** the El Marqués street-rehabilitation project on *calles San Juan del Río y Amealco* — every record nameless, all geocoded to `level_3_id=_48422011` (precision 3) — stays fragmented. The fix is list-valued location extraction so each named street geocodes to level 6: [`todos/location_level_list_extraction.md`](todos/location_level_list_extraction.md).

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

## Persistence

Persistence (the kgdb write model and the streaming pipeline) is documented in [storage.md](storage.md).
