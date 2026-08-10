# Author-context geocoding — ship it correctly (retraining + local-source validation)

**Status (2026-08-10):** the plumbing is fixed and a degradation guard is in
place, but the feature is **unvalidated in production and unexercised in
practice**. Two things must land before it can be called shipped: the geocoder's
**multi-context CER retraining**, and a **re-measurement on genuinely local
sources**. Until then it is defensively neutral, not beneficial.

Related: [deployment.md](deployment.md) (deploy sequencing) · geocoding repo
`docs/todos/kg_social_cdmx_lluvias_geo_review.md` §3.4–3.5 (design + training
stream) · [linking.md](../linking.md) (the wrapper's contract).

## How we got here

The chain is `gp3` whitelist → `record_to_article` → extraction `_author_geo` →
`geocode_location(loc, author_geo=...)` → `context_group` 2 mentions. Two links
were silently broken:

1. **`_author_geo` never reached the geocoder.** `link.py::_normalize_envelope`
   re-attached provenance from a hardcoded whitelist, so the schema Parser
   dropped `_author_geo` (and `_images`) on every record. `strategy.py` read
   `None` for the entire life of the feature. Fixed in `bd5694f` — provenance is
   now kept by rule (`_`-prefix), not by enumeration.
2. **`gp3` still doesn't send `location_author`.** `KgStreamPipeline.KG_DOC_FIELDS`
   += `location_author` remains uncommitted in `~/ocn/media/gp3`. Verified
   2026-08-10: **0 of 13** extractions in the live run carry `_author_geo`. So
   even post-fix, nothing flows yet.

## What the corpus actually says (2026-08-10 A/B)

Every fb/x document that produced an extraction, geocoded twice on the same
extracted location — bare vs. with the author location as context group 2 —
using ES `location_author` directly (i.e. a preview of post-`gp3` behavior),
caches bypassed. 53 social docs with extractions → **18 usable cases**.

| outcome | before the guard | after the guard |
|---|---|---|
| identical | 16 | 17 |
| **degraded** | **1** | **0** |
| both no-match | 1 | 1 |
| improved / rescued / lost | 0 | 0 |

**Zero improvements.** The reason is corpus shape, not the mechanism:

| | |
|---|---|
| extracted location already carries a city/state anchor | 17 / 17 |
| **anchor-less** — the scenario the feature exists for | **0** |
| author state agrees with the resolved event state | 14 |
| author state conflicts with it | 3 |

Author locations are `Distrito Federal` (10) or the useless national `Mexico`
(4). Where the author agrees with the event, context is a no-op; the only cases
carrying information are the ones where it is *wrong*.

The single degradation, verbatim:

```
https://twitter.com/IJ0ACHIM/status/2086300649302421566
text       "Mientras en CDMX se inunda Tlalpan y la gente saca el agua a cubetazos…"
author     Baja California / Tijuana (precision 3)
extracted  {city: "Ciudad de México", state: "Ciudad de México", place_name: "Tlalpan"}
bare  →  tlalpan, DE TLALPAN, Coyoacan, Distrito Federal  [p7]
ctx   →  Distrito Federal, Mexico                          [p2]
```

A fully anchored CDMX location, five precision levels lost to a conflicting
author anchor — and **the existing no-match fallback did not catch it**, because
context returned a match; just a worse one.

## The guard (implemented)

`geocode.py::_anchor_floor` + the retry in `geocode_location`: when a
context-assisted match is coarser than the location's own **admin** anchors
imply, retry bare and keep the better result (ties go to bare — context has
already shown it perturbs that record). Only `EST` (2) / `MUN` (3) set the
floor; `COL`/`CALLE`/`LUG` are excluded, since a colonia or street missing from
the KB legitimately resolves to its municipality and would otherwise fire the
retry constantly. Tests: `test_geocode_author_context.py` (guard fires on
degradation, stays out of the way on improvement and on satisfied anchors).

This makes the feature **safe**, not useful. It converts the observed harm into
a no-op; it does not create the anchoring benefit.

## Open — required before this counts as shipped

- [ ] **Multi-context CER retraining (geocoder repo).** The kg change makes
      context-group-2 input routine, so the model needs the `author_context`
      embedded bucket minted, retrained, and the behavioral test re-run
      (spec §3.5). Currently the collective matcher handles conflicting context
      by collapsing to the common ancestor — which is precisely the p7→p2 case.
- [ ] **Deploy the `gp3` whitelist** (`KG_DOC_FIELDS += location_author`).
      Until then the path is inert in production regardless of everything above.
- [ ] **Re-measure on local sources.** The CDMX-lluvias slice is national and
      big-account heavy, so the anchor-less case never appears. The upcoming
      **many-local-sources** batches (neighborhood/municipal accounts posting
      "se inundó la colonia X" with no city named) are where the feature should
      finally become visible — and where the real improvement rate, if any, gets
      measured. Re-run the A/B then; the harness is trivial to rebuild from this
      file's method (kgdb `document_extractions` ⋈ ES `location_author`, two
      `geocode_location` calls, caches off).
- [ ] **Then decide the trigger width.** Context is currently sent whenever the
      author location is state-or-finer. If the local-source batch also shows no
      upside on already-anchored locations, narrow it to anchor-less locations
      only — on the current corpus that alone would have made all 18 cases
      no-ops, guard or not.
