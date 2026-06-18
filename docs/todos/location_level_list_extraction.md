# TODO — Extract a list of locations (streets / venues / places), not a single one

**Status:** open — a **geocoder-side workaround now exists** (`refine_mentions`, see below) that covers the dominant compound-street case without the schema change; the linker/writer multi-location plumbing is still required
**Area:** `src/entities/extraction/` (schema + prompt); knock-on in `src/entities/linking/` (`strategy.py`, `geocode.py`)
**Related:** [`retrieval_linking_per_supertype.md`](retrieval_linking_per_supertype.md), [`../../src/entities/linking/readme_linking.md`](../../src/entities/linking/readme_linking.md), geocoder [`flask.md` §`refine_mentions`](/Users/oscarcuellar/ocn/geocoding/docs/flask.md)

## Geocoder-side workaround: `refine_mentions` (preferred for compound-street fields)

The geocoder's Flask service now exposes a `refine_mentions` flag on `POST /geocoder`
(geocoding repo, [`docs/flask.md`](/Users/oscarcuellar/ocn/geocoding/docs/flask.md)). Sending
`{"refine_mentions": ["CALLE"]}` makes the geocoder reassemble each context group's mentions
into an address-like string and **re-tag it through the NER**, replacing a single compound
level-6 (`CALLE`) mention with the individual streets it contains — `y`/`con`/`e` conjunctions
all split. So a `location.street` of `"Calle San Juan del Río y Calle Amealco de Bonfil"` resolves
to **both** streets instead of falling back to the municipality centroid.

This means the El Marqués / intersection case (a compound street **in one field**) is fixable
**without** the `locations: List[Location]` schema change — keep the single `location`, let
extraction concatenate the streets into `location.street`, and have the geocode wrapper pass
`refine_mentions=["CALLE"]`. Notes:

- **`["CALLE"]` only.** Do **not** refine `LUG` (venues) — the tagger over-segments proper venue
  names (`"Lunario del Auditorio Nacional"` → `"Auditorio Nacional"`). Other admin levels arrive
  clean and don't need it. Fail-soft: if the tagger returns nothing / errors, the original mention
  is kept.
- **Relevance is type-skewed:** common in `public_works` (many streets fixed at once) and
  `violence`/`security` (locations given by streets and corners); rare in `paid_mass_event`, which
  happens at named venues — so this does **not** block the persistence Step Zero on the paid fixture.
- **Integration touchpoints (still needed):**
  - `geocode.py` currently calls the `apify_client` `helpers.geocode.geocode` helper, **not** the
    Flask `refine_mentions` flag — route through the flag (or confirm the helper forwards it).
  - **Open question — does `refine_mentions` return *multiple matches* in the context group (one per
    street), or a single combined match?** The response is keyed by context group with a *list* of
    matches; verify against the live geocoder. The linker wants the **list** (each street → its own
    `level_6_id` bucket), so the wrapper must return a list of `_geo` results, not just the
    highest-precision one.
  - The linker (`strategy.py`) multi-location keys/gate and the kgdb writer's per-location
    `entity_locations` loop (proposed changes 3–5 below) are required **either way** — `refine_mentions`
    replaces the *schema/prompt* half (changes 1–2) for the compound-street case, not the
    list-handling half.

The `locations: List[Location]` schema change (below) is still the answer for events that genuinely
mention **separate** locations in distinct fields/sentences (not one compound street string).

## Problem

The entity schemas model `location` as a **single** `Location` (one country / state /
city / neighborhood / street / place). But one event often spans **several** streets,
venues, or intersections — e.g. *"intervención integral de las calles San Juan del Río
y Amealco"*. With a single slot, extraction either picks one street or, more often,
drops the street detail and the geocoder falls back to the **municipality centroid
(precision 3)**.

That coarse geocode is what blocks deduplication. The deterministic no-name merge gate
fires on `event_type ∧ (shares level_6_id or level_7_id) ∧ day overlap` — but a
precision-3 record has **no `level_6_id`/`level_7_id` to share**, so nameless
multi-street events fragment.

**Canonical case (Querétaro public_works):** the El Marqués street-rehabilitation
project on *calles San Juan del Río y Amealco* fragmented into **4 linked events** — every
record nameless (`name=None`) and geocoded only to `level_3_id=_48422011` (the
municipality). This is the level-2/3/5 weakness recorded in
[`readme_linking.md`](../../src/entities/linking/readme_linking.md): coarse-precision
nameless clusters can't be matched deterministically. See the linking docs' *Deterministic
merge gate* section.

## Goal

Extract a **list** of locations per event — every distinct street, venue, intersection,
or place mentioned for it — each independently geocodable to level 6/7. Then two records
that share **any** fine location (e.g. one mentions "San Juan del Río", another mentions
"San Juan del Río y Amealco") meet via that shared `level_6_id`/`level_7_id` and merge
deterministically, **no name required**.

## Proposed changes

1. **Schema** (`src/entities/extraction/schemas/*.json`): replace the single `location`
   slot with `locations: List[Location]` (reuse the existing `Location` composite, now
   list-valued — the loader already auto-resolves `List[...]` of composites). Each entry
   is an independently geocodable place.
2. **Prompt** (`prompts/classes/*.txt` via `prompt_generator.py`): instruct extraction to
   return **all** distinct streets / venues / intersections / places tied to the event,
   not just one. Update `meta.example` to show a multi-location list (every subfield
   present, `null` where absent — the generator depends on this).
3. **Geocoding** (`geocode.py`): geocode each `Location` in the list (the structured-input
   path already takes one dict; iterate, or add `geocode_locations(list)`). Produce a list
   of `_geo` results.
4. **Linking** (`strategy.py`): the linker already keys on *multiple* geo buckets per
   record — generalize from one `record["_geo"]` to the union over the location list:
   - registration/lookup: union the `level_N_id` buckets + grid cells across **all**
     locations;
   - deterministic gate: the "shares level_6/7" test becomes a **set-intersection** of the
     two events' fine `level_{6,7}_id` sets (match if they share any);
   - merge: accumulate the **union** of locations across merged sources.
5. **kgdb**: no schema change — `entity_locations` is already one-row-per-location
   (many locations per entity), so the persistence model already expects a list.

## Acceptance

- The El Marqués *San Juan del Río / Amealco* project links into **one** event via a shared
  street `level_6_id`, with no name and no LLM call.
- Multi-street/venue events register/look up under each of their fine locations; recall on
  the public_works fixture improves without new over-merges (audit via the case log).

## Open questions / caveats

- **Over-merge slack:** two *different* projects/incidents sharing a common street
  (+ same type + day) will merge — the accepted level-6 slack (see the same-street
  weakness in `readme_linking.md`).
- **Still precision-gated:** this only helps when the geocoder resolves the listed places
  to level 6/7. Events whose streets still resolve only to the municipality gain nothing.
- **List hygiene:** bound the list size, dedup geocoded results, and decide a primary
  location for id-minting / display.
