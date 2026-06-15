# TODO — Deterministic merge slack: no-name level-6/7 branch (+ fix precision-blindness)

**Status:** open — approved, not yet implemented
**Branch:** `geo-deterministic-linking` (continue on it; do not start from `main`)
**Area:** `src/entities/linking/strategy.py`; docs in `src/entities/linking/readme_linking.md`
**Related:** [`retrieval_linking_per_supertype.md`](retrieval_linking_per_supertype.md), [`location_level_list_extraction.md`](location_level_list_extraction.md), [`../../src/entities/linking/readme_linking.md`](../../src/entities/linking/readme_linking.md)

> This file is written to be executable cold, as a prompt in a fresh session. Read
> the linker docs (`readme_linking.md`) and `strategy.py` first; everything below
> refers to code already on the `geo-deterministic-linking` branch.

## Context (what already exists on this branch)

The geo-event linker (`src/entities/linking/`) deduplicates extracted events. Recent
work (committed A→D on this branch) added:

- **Hierarchical + coordinate retrieval** (`GeoEventStrategy`, `geo_retrieval="hierarchy"`):
  a located event registers/looks up under its `level_N_id` buckets
  (`partition_levels=(3,5,6,7)`) + a ~1 km coordinate grid cell. The geocoder wrapper
  (`geocode.py`) retains `level_1..7` + `level_1..7_id` + `coords` + `precision_level`
  on `record["_geo"]`.
- **Deterministic merge gate** (`strategy._deterministic_match`, `deterministic_merge=True`):
  skips the LLM on confident matches. Today it has **two** branches, both using
  **coordinate distance** (`_geo_within` → haversine ≤ radius):
  - *venue branch* — `scheduled_venue` supertypes only (`paid_mass_event`): coords ≤ `r7_m`
    (75 m) + both `precision_days ≤ 1`, no name.
  - *named branch* — any supertype: `name_similarity ≥ name_tau` (0.65) + coords ≤ `r6_m`
    (150 m) + both `precision_days < det_precision_days` (3).
  Helpers: `DeterministicPolicy` dataclass + `_SUPERTYPE_POLICY` (only `paid_mass_event`
  is `scheduled_venue`), `_geo_within`, `_geo_distance_m`, `_date_overlap`,
  `_cand_window`. Name similarity is `text_util.name_similarity` (character-trigram).
- **Case log** (`EntityLinker(case_log_path=...)`): one JSONL line per record to
  `data/.runlogs/linking_cases_<stem>.jsonl` (candidates with `geo_dist_m`/`name_sim`,
  decision `path` ∈ {no_candidates, deterministic, llm}, `decision`).

## Problem this change fixes

1. **Precision-blindness (a real bug).** `_geo_within` uses haversine distance whenever
   both records have coords. A **precision-3** record's coords are the *municipality
   centroid*; if that centroid happens to fall within `r6_m`/`r7_m` of a precise event,
   the gate fires and a municipality-only record auto-merges into a specific one.
   Demonstrated: a precision-3 record 70 m from a precision-7 event merged. Distance is
   only a *proxy* for "fine enough"; it isn't precision-aware.
2. **Missing slack for nameless fine events.** The no-name path is `paid_mass_event`-only,
   so a nameless non-venue event (e.g. public_works) at street/place precision can't
   auto-merge even when it's obviously the same — it falls to the LLM, which under-merges.

## The change (agreed design)

Add **one no-name branch, for all supertypes**, keyed on **level-id sharing** rather than
coordinate distance:

> Merge deterministically (skip the LLM) when a candidate has the **same `event_type`**
> (already guaranteed by the partition) **AND shares a non-empty `level_6_id` OR
> `level_7_id`** (same street or same place) **AND** the date windows overlap (tight
> slack) **AND** both dates are *extracted* (not publication fallback). **No name
> required.**

Why this is correct and safe-by-construction: a record can only *share* a `level_6_id`/
`level_7_id` if **both** geocoded to level 6/7 — so it is inherently precision-aware and
**fixes the precision-blindness bug for free** (a coarse, level-3 record has no fine id to
share, so it can never trigger; no separate `precision_level` guard needed).

**Decisions baked in (per the owner):**
- **Level 6 (street) is included unconditionally** — accept that two *different* same-type
  events on the same street/day will merge (e.g. two accidents). Documented as a weakness.
- **Levels 2/3/5 get no deterministic path** — coarse records keep going to the LLM.
  Documented as a weakness; the proper fix is [`location_level_list_extraction.md`](location_level_list_extraction.md).

**Named/venue branches:** the new level-id-share branch **subsumes the venue branch**
(a venue is a `level_7` place) — remove the venue branch. For the **named branch**: it is
the only remaining use of coordinate distance and is still precision-blind; either (a)
**remove it** (simplest — names still help the LLM downstream), or (b) **keep it but add a
`precision_level ≥ 6` guard** on both sides. Recommended default: **(a) remove it** — the
owner's instruction was "match regardless of names" on shared level 6/7, and dropping the
coords-distance path eliminates the last precision-blind code. `name_similarity`/`text_util`
then becomes unused by the gate (leave the module; it's cheap and the list-location work
may revive a name path).

## Implementation pointers

- Edit `strategy._deterministic_match`: replace the two distance-based branches with the
  single level-id-share branch. A helper like
  `_shares_fine_id(ga, gb) -> bool` (intersect non-empty `level_6_id`/`level_7_id`) reads
  cleanly. Keep the `prep.window.source == "extracted"` guard and the `_date_overlap`
  check (slack ≈ 1 day).
- `DeterministicPolicy` can shrink (drop `r6_m`/`r7_m`/`name_tau` if the named branch is
  removed; keep a day-slack knob). `scheduled_venue` is no longer needed.
- `_geo_within`/`_geo_distance_m` become unused if the named branch is removed — delete or
  leave with a note.
- All fields you need are already on `record["_geo"]` (`level_6_id`, `level_7_id`,
  `precision_level`) and on candidate events in `self.events`.

## Weaknesses to document (in `readme_linking.md`, deterministic-gate section)

1. **Same-street collisions (level 6):** two distinct same-type events on the same street
   and day will deterministically merge (e.g. two accidents). Accepted slack.
2. **Coarse-precision under-merge (levels 2/3/5):** records that geocode only to
   municipality/city/neighborhood share no `level_6/7_id`, so nameless coarse clusters are
   never matched deterministically — they rely on the LLM and may under-merge. **Canonical
   example:** the El Marqués street-rehabilitation project on *calles San Juan del Río y
   Amealco* — every record nameless, all geocoded to `level_3_id=_48422011` (precision 3),
   fragmented into 4 events. The fix is list-location extraction
   ([`location_level_list_extraction.md`](location_level_list_extraction.md)), **not** this
   change (this change cannot help a record with no fine id).

## Verification

Geocoder must be reachable (`GEOCODING_URL=http://localhost:8090/geocoder`,
`NLP_URL=http://localhost:8210/tag`). The frozen extracted fixture is
`data/extracted_raw/geo_qro_public_works_event.json` (no re-extraction needed).

1. **Smoke** (`python3 -c`): build `GeoEventStrategy(geocode=False)`; confirm two records
   that share a `level_6_id` (or `level_7_id`) + same `event_type` + same day merge via
   `_deterministic_match`; confirm a precision-3 record (no `level_6/7_id`, centroid coords
   near a precise event) does **not** merge (the bug is gone).
2. **Run** the fixture and compare to Phase D:
   ```
   GEOCODING_URL=http://localhost:8090/geocoder NLP_URL=http://localhost:8210/tag \
   LINK_INPUT_STEM=geo_qro_public_works_event LINK_OUTPUT_STEM=geo_qro_public_works_event__detslack \
   python3 src/entities/linking/run_linking.py
   ```
   Check: event count + multi-source count stay sane (no recall collapse, no mass
   over-merge), the `path="deterministic"` count rises, and **audit every deterministic
   merge in the case log** (`data/.runlogs/linking_cases_*__detslack.jsonl`) — each should
   share a real street/place. El Marqués stays fragmented (expected — precision 3). The
   Paseo de México sinkhole should remain merged.

## Commit plan

One commit on `geo-deterministic-linking` (code + the two weakness docs together), e.g.
`linking: no-name level-6/7 deterministic branch (precision-aware via level-id sharing)`.
End the message with the standard `Co-Authored-By` trailer. Also commit the two TODOs
written alongside this work (`location_level_list_extraction.md`, `deterministic_match_slack.md`)
and the README roadmap links if still uncommitted.
