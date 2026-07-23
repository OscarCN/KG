# Handoff from the geocoding side — 2026-07-22/23 changes & asks

Context: the geocoding repo shipped the street (L6) enrichment cascade, ran a
128-location shakedown over `data/geocode_street_test_locations.json`, and
hardened the alias/training gates after the fallout. Full detail:
geocoding repo `docs/status/pipeline.md` + `docs/status/data-quality.md`
(2026-07-22/23 entries). What matters on the kg side:

## Asks (kg-side work)

1. **Venue lists cannot be worked around geocoder-side.**
   `location.place_name` values like *"Ángel de la Independencia, Zócalo de la
   Ciudad de México"* (march routes) pass the single-venue rule textually and
   used to write the whole route string as a KB name variant of the first
   venue (now blocked by a name-hygiene gate; the two Ángel variants were
   removed from the KB). But blocking ≠ resolving: those events stay coarse.
   `refine_mentions` can NOT be used for venues (`LUG` over-segments — see
   [`location_level_list_extraction.md`](location_level_list_extraction.md)),
   so venue lists need the extraction-side split (the `locations: List` schema
   change) or at minimum a hold-out flag. A march route is legitimately two
   geocodable venues + N streets.

2. **Send `refine_mentions: ["CALLE"]` contextually** (the integration
   touchpoint already noted in
   [`location_level_list_extraction.md`](location_level_list_extraction.md)).
   Verified live this week: *"Avenida Juárez e Hidalgo"* and *"calle Granada y
   avenida Del Trabajo"* split correctly and the second one upgraded to a
   correct L6. Caveat: a refined multi-street request can also *downgrade*
   honesty-wise — the CER may resolve a wrong-city same-name street (the urban
   homonym weakness, being addressed with training data on our side) — so
   treat refined L6 results as candidates for the repair gate, not blind
   truth.

3. **Confirm `91988c1` (house number kept out of the CALLE mention) is
   deployed in the worker.** The geocoder now also strips defensively, but
   the mention should arrive clean.

## Heads-up (no kg action, but affects kg data)

4. **A kgdb repair sweep is pending from the geocoding side**: ~30 events
   from the street batch now resolve at L6+ (e.g. entity 131 — stored as
   "CENTRO, Cuauhtémoc, DF", now resolves to BENITO JUAREZ ORIENTE, San Juan
   del Río, Qro). Writes will follow the runbook (`entity_locations` +
   `metadata._geo` + the geocode disk cache — skipping the cache re-poisons
   the stream), gated by the street-name gate + containment companion.

5. **Some events may re-geocode COARSER now — that's correct.** 16 wrong KB
   aliases were removed this week (e.g. "Basílica…" no longer aliases to its
   museum, route strings no longer alias to the Ángel). Events that were
   "precise" via those aliases were precisely wrong.

6. **The wrong-geocode feedback file**
   (`data/geocode_wrong_geocoded.json`) is a useful channel — being reviewed
   on the geocoding side now; worth formalizing (append-only + a `reported_at`
   field) if the worker keeps producing it.

## Review of `data/geocode_wrong_geocoded.json` (2026-07-23, geocoding side)

17 rows / 14 unique locations reviewed. Routing:

- **`wrong_match` corridor rows (644/824…)**: real and ours — the CER
  corridor-homonym weakness amplified by the imp-2 exemption ("autopista
  México-Querétaro" with no state anchor serves AUTOPISTA MEXICO-PUEBLA).
  Tracked on the geocoding model line (homonym training bucket + a possible
  serving guard: imp-2 matches on an L1-only anchor should need exact-grade
  name agreement). Do NOT trust current L6 answers for anchor-less corridor
  mentions.
- **`refine_context_mismatch`**: three distinct sub-cases —
  1. genuine wrong-city street homonyms (#373, #1186): ours, same bucket;
  2. **your consistency heuristic is too strict**: "all street matches in the
     same municipality" false-alarms on mun-spanning avenues (#1162, Paseo de
     la Reforma legitimately spans Cuauhtémoc + Miguel Hidalgo). Test
     containment against the matched colonia/mun instead of mun equality;
  3. **refine correcting a wrong plain match** (#1910/#1249/#1681/#1844): the
     plain L5 "AÑO DE JUAREZ, Iztapalapa" is itself a colonia homonym miss for
     colonia Juárez (Zona Rosa) — the refined Cuauhtémoc streets are RIGHT.
     Don't auto-prefer plain on mismatch; flag for the repair gate.
- **`refine_degraded` (#1385)**: adopt the fail-soft rule — never accept a
  refined result whose best precision is below the plain result's.
