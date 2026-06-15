# TODO — Per-supertype retrieval & linking strategy

**Status:** geo strategy v1 **implemented** (see [Geo strategy v1](#geo-strategy-v1) and the decision log below); per-supertype generalization still open
**Area:** `src/entities/linking/`
**Related:** [`src/entities/readme_entities.md`](../../src/entities/readme_entities.md), [`src/entities/linking/readme_linking.md`](../../src/entities/linking/readme_linking.md)

## Problem

What makes an entity **identifiable** — and therefore how we retrieve linking
candidates and decide whether two records are the same — differs by ontology
class. The current linker (`src/entities/linking/link.py`) hardcodes a single
strategy that only fits **events with a clean geographic component**:

- **Candidate index:** `(event_type, level_2_id, date_key) → {event_ids}`
- **Disambiguation:** one LLM call over `name`, `description`, structured
  `address`, `date`.

This breaks down for entity types whose identity does not hinge on geography or
on a date:

| Supertype (example) | Identifying attributes | Why the current strategy fails |
|---|---|---|
| **events with location** (current) | class + location + date-time | — (this is the implemented case) |
| **persons / organizations / products** | name and/or description | no meaningful date or location to filter on |
| **legislative_initiative** | region (location) + name + description | location is jurisdiction, not an incident point; name/description carry most of the identity |
| **real estate developments** | location + name | no date; dedup is name-at-a-place |
| **person's actions** — `said` (statement/claim/endorsement/assertion) | **actor** + description (claim content) + date-time | no geo; dedup is *who said it* + *what was said* + *when* |
| **person's actions** — `did` (attended/organized/gave-interview) | **actor** + action-class + date-time + *optional* location | geo is weak/optional; actor identity is the primary key |

Two cross-cutting constraints make a naive "embed everything + LLM decide"
approach unworkable:

1. **Candidate retrieval must match the attribute.** Embeddings retrieve poorly
   on geospatial data and on uncommon/proper names. Geo wants a spatial index,
   names want LSH/token overlap, free text wants embeddings, and resolved
   entities (e.g. a person `actor_id`) want exact-match. One method does not fit
   all attributes.
2. **LLM disambiguation is costly.** It should run only after a cheap,
   high-precision candidate filter has shrunk the set — ideally to a handful.
   For `person_actions`, an exact `actor_id` filter (the actor resolved to a KB
   person entity) is the cheap primary narrowing step before any embedding or
   LLM call.

## Prerequisites — what the strategy abstraction must own

Design-review findings that the abstraction has to satisfy *before* it is
built. Each one is a place where a naive "ordered list of `(attribute, method)`"
spec under-delivers:

1. **Full lifecycle, not just lookup.** A strategy owns
   `enrich → register → lookup → adjudicate → merge → reindex`. Today
   `_merge_event` (fillna policy, date widening, location promotion, post-merge
   reindexing) is as supertype-specific as retrieval — leaving merge out keeps
   half the polymorphism hardcoded.
2. **The spec must express the current geo behaviour** — its own acceptance
   case. That requires per-attribute **fallback chains** (extracted date →
   publication date, each with its own slack), **dual-window registration**,
   per-method **parameters** (slack days, caps), and a registration-side
   declaration, not just a lookup-side one. The [Geo strategy v1](#geo-strategy-v1)
   section below is this litmus test, written out.
3. **Enrichments are declared by the strategy.** `level_2_id` and `actor_id`
   are not extracted fields — they are *produced* (geocoder call,
   person-resolution). A strategy declares which enrichments run, so non-geo
   supertypes skip geocoding (cost), and `person_actions` declares its
   dependency on actor resolution. Actor resolution also imposes a
   **per-document linking order** (persons before the actions that reference
   them) — a constraint on the streaming runner, not just `link.py`.
4. **Two-stage retrieval model.** Conjunctive **partition keys** (exact-ish:
   type, `level_2_id`, `actor_id`, day-keys) compose into index keys; **fuzzy
   retrievers** (LSH, embedding-kNN, geo-proximity) return ranked sets *within*
   a partition and cannot be intersected the same way. Candidate **caps** and
   deterministic **short-circuits** (accept/skip without LLM) are declared per
   stage.
5. **Index behind a protocol.** Matchers are `register(record)` /
   `lookup(record) → ids` against a `CandidateIndex` protocol, so the current
   in-memory dict and the future kgdb-backed retrieval
   (`event_properties` ∕ `entity_locations` ∕ `entity_types`) are
   interchangeable. Sanity-check every strategy-table row against the kgdb
   schema now — e.g. `name_token`/LSH retrieval has no kgdb-side support yet.
6. **Explicit skip, no silent defaults.** "Not linked" (themes, today) is a
   *declared* strategy; a record whose supertype has no schema or no strategy is
   a logged drop — `_category_for` currently defaults missing schemas to
   `"event"`, which silently routes them down the geo path.
7. **`meta` vs registry split.** Field selections and method *names* may
   eventually live in schema `meta` (semantic, travels with the schema); method
   *implementations and infra parameters* stay in a code registry (deployment
   concern — a schema must not assert `embedding_knn` when no vector store is
   wired). v1 keeps everything in the code registry; push into `meta` only once
   the spec language is proven.
8. **Cache stability.** The sha256-keyed LLM cache doubles as the regression
   harness: keep `_llm_payload` byte-identical for geo events through the
   refactor, or replays re-bill and may return different answers that look like
   regressions.
9. **Class-family overrides** = a supertype strategy plus an optional
   **discriminator field** declared by that strategy (`said`/`did` keyed off an
   action-kind field) — not a per-class registry; per-class is too granular and
   diverges from one-schema-per-supertype.

**Prior art:** a removed prototype linker (git history, removed in commit
`953546f`) implemented the persons/orgs/products row — entity candidates by
same `entity_type` + shared name tokens, LLM adjudication over `name` +
`description`. Reuse its approach when that row lands.

## Goal

Define, **per supertype (and where needed, per class-family)**, a declarative
**retrieval strategy** and **linking (identification) strategy** expressed as a
*set of attributes + the method used on each*, so that:

- Adding or changing a supertype's linking behaviour is **configuration, not
  code**.
- The retrieval and linking logic is **modular** — a small set of reusable
  matchers (exact, date-overlap, geo, name-token/LSH, embedding-kNN) composed by
  the declared strategy.

Sequencing: **define and harden the geo case first** (it exercises partitions,
fallback chains, dual registration, merge and reindex), then generalize the
interface from it.

## Geo strategy v1

Normative definition of identification for geo-events. **Implemented** in
`src/entities/linking/` as `GeoEventStrategy` (`strategy.py`) +
`CandidateIndex` (`index.py`) + state catalogue (`mx_states.py`), orchestrated
by a supertype-agnostic `EntityLinker` (`link.py`) — every fix below is a
strategy constructor parameter with a legacy value that reproduces the
pre-refactor behaviour for regression runs. Implemented behaviour is
documented in [`readme_linking.md`](../../src/entities/linking/readme_linking.md);
the decision log at the end of this section records what changed against this
spec during implementation.

Items marked **(current)** described the pre-refactor behaviour; items marked
**(fix)** are the v1 changes (all landed).

### Identity model

Two geo-event records denote the **same event** iff:

1. **same class** — `event_type` exact match *(partition)*;
2. **same place** — state-level (`level_2`, catalogue-normalized) partition
   for retrieval; address-level comparison left to adjudication
   *(partition + adjudication)*;
3. **overlapping time** — precision-aware date window with declared fallbacks
   *(partition)*;
4. **co-referent content** — `name`/`description` judged by the LLM
   adjudicator *(adjudication)*.

### Stages

- **Enrich** — geocode the structured `Location` → `_geo`
  (`level_2`, `level_3`, `precision_level`, coords). **(current)**
  **(fix)** when the geocoder yields no `level_2` but `location.state` text
  exists, normalize the state string through a static catalogue (32 entities,
  deterministic, no service call) and record provenance as
  `_geo_source ∈ {geocoder, state_catalogue, none}` — shrinks the no-geo
  bucket and separates "no location extracted" from "geocode failed".
- **Partition keys** — `(event_type, geo_key, day_key)` where `geo_key` is the
  catalogue-normalized state. The empty geo_key ("noloc") bucket is the
  explicit last geo tier, not an accident; located lookups also probe it
  (noloc bridge — see decision log). **(implemented)**
- **Time window** — tiered, provenance recorded as `_date_source`:
  1. extracted `date_range` → slack
     `max(EXTRACTED_DATE_SLACK_DAYS, precision_days)` — **(fix)** extraction
     already produces `precision_days` on `DateRangeFromUnstructured` ("in
     March" ⇒ ~30, exact date ⇒ 0–1); the linker currently ignores it and
     applies a fixed ±1 day, under-matching imprecise dates.
  2. publication date (`date_created`) → symmetric ±2-day slack.
     **(decision: kept symmetric.)** A per-supertype directional window was
     considered and dropped — most types can be referenced before *or* after
     they happen (concerts, protests, closures are announced and reported),
     so a directional prior doesn't hold per supertype.
  3. neither → drop (`event_no_date_no_pub`). **(current)**
- **Register** — under **all available windows** (extracted *and*
  publication), so a dated record and a publication-only record about the same
  event still meet. This cross-provenance bridge is deliberate. **(current)**
- **Adjudicate** — one LLM call over `name`, `description`, `address`,
  `date`, `publication_date` **(current)**; **(fix)** cap the candidate list
  at N (most-recent-first) instead of unbounded; empty list short-circuits to
  *create* without a call **(current)**. Optional deterministic accept
  (skip LLM): normalized-name equality ∧ same `level_3` ∧ extracted-date
  overlap on both sides.
- **Merge** — append `source_ids` (de-duped); fillna `name`, `description`,
  `context`, `status`; keep earliest `publication_date`; promote `location`
  when more populated within the same geo partition; re-register under new
  day-keys. **(current)** Date-range policy: see imprecision (3) below.

### Known imprecisions → lean fixes (priority order)

1. **`_date_keys` long-range bug.** Ranges over 365 days enumerate only the two
   *endpoint* day-keys, so overlap detection misses every date in between.
   Fix: clamp with explicit policy (e.g. cap the enumerated window at the first
   K days and log), or move to an interval check within the partition. This is
   a correctness bug — fix first.
2. **`precision_days` ignored** (time-window tier 1 above) — data already
   extracted, zero extra cost, directly reduces missed merges on approximate
   dates.
3. **Unbounded date widening on merge.** `start=min, end=max` means one wrong
   merge or one imprecise extraction permanently widens the canonical window,
   attracting more candidates → snowball-merge drift. Fix: prefer the most
   *precise* source range (smallest `precision_days`) as the canonical window;
   keep per-source ranges on the record so widening is recoverable.
4. **No-geo bucket conflation** — the `state_catalogue` fallback and
   `_geo_source` provenance (Enrich, above).
5. ~~**Symmetric publication slack**~~ — **dropped** (see time-window tier 2
   above: no per-supertype directional prior holds).
6. **Unbounded LLM candidate list** — the cap; if a partition routinely
   exceeds it, refine with `level_3` (city) as a secondary key before falling
   back to chunked LLM calls.

### Decision log (implementation, 2026-06)

- **All fixes landed as `GeoEventStrategy` parameters** with legacy values for
  regression (`geo_partition_field="level_2_id"`, `clamp_long_ranges=False`,
  etc.). Regression vs the pre-refactor fixture: 34/35 events equivalent; the
  35th was junk produced by the legacy silent default (next bullet).
- **The bug was worse than spec'd:** besides the `level_2_id` partition never
  firing, the legacy category-defaults-to-`event` path was linking
  *schema-less* records — the fixture contained 16 `public_infrastructure`
  records (pre-rename supertype) that merged into a nameless, dateless
  "event". Both are now explicit: partition on `level_2`, schema-less
  supertypes drop as `no_schema`.
- **Noloc bridge added** (`probe_noloc_bucket=True`): located lookups also
  probe the `""` bucket, so an event first seen without a location can be
  matched by later located mentions. The reverse direction is impossible (a
  noloc record can't know which partition to probe) — that residual recall
  loss vs the accidental single-bucket legacy behaviour is the partition's
  accepted trade-off (observed once on the test fixture: a no-location
  festival mention arriving after its located cluster stays separate).
- **Geocoder quirk observed:** the wrapper picks the highest-`precision_level`
  match regardless of state agreement, so a record whose extracted `state`
  text says one state can partition under another (seen: Amealco, Querétaro →
  `estado de mexico`). Deterministic per location input (cached), so
  partitioning stays consistent; revisit in the geocoder wrapper if it starts
  splitting real clusters.
- **Deferred:** deterministic name-equality short-circuit (skip the LLM on
  normalized-name + `level_3` + date match); `level_3` refinement when a
  partition exceeds the candidate cap.

### Decision log (geo v2 — hierarchical retrieval + deterministic gate, 2026-06)

Both deferred items above **landed**, generalized (see
[`readme_linking.md`](../../src/entities/linking/readme_linking.md)):

- **Hierarchical + coordinate retrieval** (`geo_retrieval="hierarchy"`). A located
  event registers/looks up under its `level_N_id` buckets (`partition_levels=(3,5,6,7)`)
  **and** a ~1 km coordinate grid cell (lookup probes the 8 neighbors) — *not* a
  shared state-wide bucket. This kills the single-state degeneracy while the grid
  carries cross-municipality recall (same place, disagreeing `level_3_id`). The
  geocoder wrapper now retains `level_N_id` (the keys) and `coords`.
- **Deterministic merge gate** (`deterministic_merge`, per-supertype
  `DeterministicPolicy`). Skips the LLM on confident matches: a venue branch
  (`scheduled_venue` supertypes — coords ≤ ~75 m + exact dates, no name) and a
  named branch (`name_similarity ≥ 0.65` + coords ≤ ~150 m + tight dates). Geo is
  haversine, `level_N_id` equality as the coords-less fallback.
- **Description-centric LLM payload.** Identity judged on described facts, not a
  privileged name (most records have none); the name is folded into a
  description-led `identification` field.
- **Case log** (`EntityLinker(case_log_path=...)`): per-record JSONL of candidates
  (`geo_dist_m`, `name_sim`) + decision path — the audit/tuning trail.
- **Observed limit (Querétaro public_works):** the deterministic gate barely fires
  because 72/107 records are **nameless** and most geocode only to precision 3
  (municipality centroid). The residual under-merges (e.g. one El Marqués street
  project fragmenting into 4) are **extraction-quality bound** (no name + coarse
  location), not retrieval/adjudication. A no-name branch for non-venue supertypes
  would need a `precision_level ≥ 6` gate to be safe — left as a data-driven
  follow-up once extraction yields names / finer coordinates.

### Pipeline cleanup (code shape for v1)

- Split `link.py` into: a `CandidateIndex` protocol (`register`/`lookup`), a
  `GeoEventStrategy` object declaring everything in *Stages* above (enrichments,
  partition keys, window tiers + parameters, payload fields, merge policy,
  short-circuits), and a thin `EntityLinker` orchestrator that is
  supertype-agnostic.
- Normalize the record **envelope** at the boundary: `_source_id`,
  `_supertype`, and the `date_created`-vs-`publication_date` duality are
  currently stripped/re-added ad hoc; carry them in a meta envelope alongside
  the schema-validated record.
- Replace the `_resolve_window` 4-tuple with a small
  `DateWindow(start, end, slack_days, source, precision_days)` value.
- Missing schema / missing strategy ⇒ logged drop (kills the `"event"` default
  in `_category_for`).
- Keep the geo-event `_llm_payload` byte-identical (prerequisite 8).

All of the above is implemented (`index.py`, `strategy.py`, `mx_states.py`,
slimmed `link.py`).

## Scope

### v1
- ~~Implement the [Geo strategy v1](#geo-strategy-v1) spec~~ — **done** (see
  decision log above; regression-checked against `data/linked/` fixtures).
- Add an `actor_id`-keyed strategy for `person_actions` (`said` / `did`) on the
  same interface. **(open)**
- Do **not** build general-purpose retrieval infrastructure (vector DB, etc.)
  yet — `said` content-dedup can lean on the LLM adjudicator over a tiny
  actor+date-filtered candidate set.

### v2 (later)
- Generalize retrieval to a declared `(attribute → method)` spec read from the
  schema `meta` (e.g. `meta.linking` / `meta.retrieval`) — only once the v1
  registry has proven the spec language (prerequisite 7).
- Introduce the embedding/vector retrieval path for description-driven types
  (persons, organizations, products) once the interface is stable.

### Strategy table (target)

| Supertype / family | Retrieval (candidate filter) | Adjudication fields |
|---|---|---|
| geo events (current) | `event_type` (exact) ∧ (`level_N_id` buckets + coordinate grid, hierarchical) ∧ `date_overlap` (tiered, precision-aware); deterministic gate (coords+name+date) before the LLM | `identification` (description-led, name folded in), address, date, publication_date |
| `legislative_initiative` | `entity_type` (exact) ∧ `level_2_id`/region (exact) ∧ `name_token` | name, description, jurisdiction |
| persons / orgs / products | `entity_type` (exact) ∧ `name_token`/`lsh` (+ `embedding_knn` in v2) | name, description |
| real estate developments | `level_2_id` (exact) ∧ `name_token` | name, location |
| `person_actions` · `said` | `actor_id` (exact) ∧ `date_overlap` | actor, claim/description (content), date |
| `person_actions` · `did` | `actor_id` (exact) ∧ `date_overlap` ∧ *optional* `level_2_id` | actor, action class, date, location |

## Open questions

- **`actor_id` resolution:** `person_actions` linking presumes the actor is
  already resolved to a KB **person** entity during extraction/linking. That
  dependency (person-as-entity + actor field on the action schema + the
  per-document ordering from prerequisite 3) must land first. See the
  `person_actions` / stream-task work.
- **Cross-source `said` dedup without geo:** with no geographic narrowing,
  statement dedup rests on `actor_id` + date + semantic match of content.
  Verify recall is acceptable on a small actor+date candidate set before
  deciding whether v2 embeddings are needed here.
- **Candidate cap value (N)** and the `level_3` refinement threshold for geo —
  cap shipped at 12 (untriggered on current fixtures); revisit both from
  larger-run statistics.

*(Resolved into prerequisites: strategy home → code registry now, `meta`
later (7); class-family granularity → supertype + discriminator field (9);
cost guardrails → declared short-circuits and caps per stage (4).)*

## Acceptance (v1)

- ✅ `link.py` no longer hardcodes the `(event_type, level_2_id, date_key)`
  index shape; strategy is selected via the category registry, candidates flow
  through the `CandidateIndex` protocol, and the strategy owns
  enrich/register/lookup/adjudicate/merge/reindex.
- ✅ Geo-event linking output unchanged with legacy parameter values
  (regression-checked against `data/linked/ayuntamiento_tst.json`, byte-stable
  LLM payloads); v1 defaults then enabled and the diff reviewed (decision log).
- ⬜ `person_actions` (`said` / `did`) links by `actor_id` + date with the
  correct adjudication fields — blocked on actor resolution (open question
  above).
- ✅ Themes and schema-less supertypes are explicit declared skips/drops, not
  fall-throughs.
- ✅ Behaviour documented in `readme_linking.md`; README and entity docs point
  here.
