# TODO — Geocoder mismatches: homonym streets, discarded anchors, KB gaps

**Status:** open — evidence collected 2026-08-30/31 (CDMX marathon corpus); every pair below
verified live against the geocoder (`localhost:8202`, production KB)
**Area:** `src/entities/linking/geocode.py` (`_build_mentions`, `_pick_best_match`); geocoding
repo (KB content, collective matcher)
**Related:** [`location_level_list_extraction.md`](location_level_list_extraction.md) (multi-site
plumbing that multiplies exposure), [`author_context_geocoding_rollout.md`](author_context_geocoding_rollout.md)
(context-group machinery), [`geocode_document_context.md`](geocode_document_context.md)

## Context

Re-geocoding the 2026-08-30 marathon closure corpus (route-shaped multi-site events; see the
cc review record) produced **9 recurring wrong-homonym street matches** out of ~30 distinct
sites — every one a CDMX street resolved to a same-named twin in the wrong alcaldía, 6–10 km
off the route. A missing pin is honest; a coarse pin is honest; a wrong pin asserts a closure
where nothing is closed. The multi-site pipeline raises exposure ~10× (ten sites per event,
and roundup articles are exactly the bare-street-list genre).

## The mismatch pairs (extracted → matched, deduped; each recurred across 2–7 events)

| # | extracted (all with EST/MUN = CDMX) | matched | truth (route context) |
|---|---|---|---|
| 1 | street=Av. Insurgentes / Insurgentes Sur-Norte | INSURGENTES, **Iztapalapa** p6 | Insurgentes Sur/Centro (BJ/Cuauhtémoc) |
| 2 | street=Av. Nuevo León, **place_name=Parque España** | NUEVO LEON, **Xochimilco** p6 | Condesa (Cuauhtémoc) |
| 3 | street=Ejército Nacional, **neighborhood=Polanco** | EJERCITO NACIONAL, **Iztapalapa** p6 | Polanco (Miguel Hidalgo) |
| 4 | street=Calle/Av. Oaxaca | OAXACA, **La Magdalena Contreras** p6 | Roma (Cuauhtémoc) |
| 5 | street=Calle/Av. Sonora | SONORA, **Venustiano Carranza** p6 | Roma/Condesa (Cuauhtémoc) |
| 6 | street=Ignacio Ramírez, place_name=Plaza de la República (sometimes) | IGNACIO RAMIREZ, **Iztacalco** p6 | Tabacalera (Cuauhtémoc) |
| 7 | street=16 de Septiembre | 16 DE SEPTIEMBRE, **Iztapalapa** p6 | Centro Histórico |
| 8 | street=Av. 20 de Noviembre | 20 DE NOVIEMBRE, **Venustiano Carranza** p5 | Centro Histórico |
| 9 | street=Florencia | FLORENCIA, **Tlalpan** p6 | Zona Rosa (Cuauhtémoc) |

Prior production specimen of the same class: event 11956 ("XLIII Maratón…") pinned in
**Tláhuac** via the dirty KB entry below — through the normal pipeline, before any of this
tooling existed.

## Root causes — four distinct mechanisms (verified by replaying exact payloads)

### A. The client keeps an incoherent fine match over a coherent coarse one (`_pick_best_match`)

Pair #3 payload (`COL=Polanco` + `CALLE=Ejército Nacional`) returns **two matches**:
`POLANCO CHAPULTEPEC, Miguel Hidalgo` (p5, **correct area**) *and* `EJERCITO NACIONAL,
Iztapalapa` (p6, wrong twin) — ~12 km apart. `_pick_best_match` keeps one match by
precision, so the wrong p6 street beats the right p5 colonia. **The geocoder returned the
evidence needed to reject the street match; the client discarded it.** Fix is kg-side, no
retraining: when a group's matches are geographically inconsistent, prefer the street
consistent with the matched COL/LUG, else fall back to the coherent coarser match.

### B. Missing KB entries silently drop anchors

Pair #2: `LUG=Parque España` returns **no match at all** (KB gap — a canonical Condesa
landmark), so the street resolves unconstrained and picks Xochimilco. No error, no signal.
Likely same class for some `Plaza de la República` sends (unverified). KB-content item for
the geocoding repo; client-side, an unmatched LUG/COL on a homonym-prone street could also
demote the result to coarse instead of trusting the bare street match.

### C. Bare street names have no disambiguating signal — but their siblings do

Pairs #1, 4, 5, 7, 8, 9 carry only EST/MUN/CALLE. Two aggravators:
- **`MUN="Ciudad de México"` is dead weight**: CDMX's level-3 units are the alcaldías; no
  municipio is named "Ciudad de México", so the mention matches nothing and the only live
  constraint is the state — within which every homonym is fair game. (`_build_mentions`
  should drop/remap MUN when it duplicates the state.)
- **Each site is geocoded in its own request.** The sibling sites of the same event (the
  route list: …Glorieta de Insurgentes → Oaxaca → Nuevo León → Sonora → Chapultepec…)
  are exactly the context that disambiguates — the Oaxaca between Insurgentes and
  Chapultepec is the Roma one. Batch an event's sites into **one request** (the mention
  set, or context groups as in the author-geo machinery) and/or post-filter by geometric
  coherence: in this corpus every wrong twin was a 6–10 km outlier from the site cluster.

### D. Dirty KB entries

- A p7 entry `"zocalo"` whose formatted name is `"Av.Tláhuac #4515 col. Lomas estrella"`
  under a Cuauhtémoc-Centro geoid — it also produced the 11956 Tláhuac pin.
- Concatenated-mention entries like `"Angel de la Independencia al Zocalo capitalino"`
  stored as a single place. KB cleanup items.

## Separate finding — extraction drops article context (NOT a geocoder bug)

The infobae piece (event 11857's source) annotates segments explicitly — *"Avenida
Chapultepec y Calle Oaxaca: **conexión hacia la colonia Roma y Condesa**"*, *"…Ejército
Nacional: tramo que comprende la **zona de Polanco**"* — and the extraction emitted all
eight site dicts **bare** (every `neighborhood` null). The posta piece's extraction, by
contrast, did carry `Polanco` / `Centro Histórico`. So context capture is inconsistent:
inline colonia mentions survive; the *"street: colonia"* prose-annotation pattern is
dropped. The other four marathon roundups contain no colonia names at all near the streets
(bare lists), so sibling context (mechanism C) remains necessary regardless. Prompt-side
item — belongs with `location_level_list_extraction.md`, recorded here because it caps what
any geocoder fix can recover.

## Proposed fix order

1. **`_pick_best_match` coherence rule** (A) — kg-side, small, kills the worst class
   (confidently-wrong despite correct evidence in the response).
2. **Drop/remap the CDMX MUN mention** (C) — `_build_mentions` one-liner.
3. **Batch sibling sites per event + coherence outlier filter** (C) — kg-side wrapper
   around the multi-site geocode loop; prerequisite-free once `_locations` flows.
4. **KB additions/cleanup** (B, D) — geocoding repo: Parque España; audit `zocalo`-class
   dirty rows.
5. Optional guard: unmatched LUG/COL on a homonym street ⇒ degrade to coarse (B).

## Test corpus

The 9 pairs above, replayable verbatim: build the payload with `_build_mentions` and POST
with `refine_mentions: ["CALLE"]`. Acceptance: #1–#9 resolve to the route alcaldías
(Coyoacán / Álvaro Obregón / BJ / Cuauhtémoc / MH) or degrade to coarse — never to the
wrong twin. Regression: the correctly-matched marathon sites (Reforma, Chapultepec,
Masaryk, Molière, Julio Verne, Glorieta de los Insurgentes p7, Estadio Olímpico p7) must
keep their matches.
