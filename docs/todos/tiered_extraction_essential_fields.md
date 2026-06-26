# TODO — Tiered extraction: on-demand enrichment (full) pass

**Status:** open — essential-by-default extraction **shipped**; the on-demand enrichment trigger is the remaining work
**Area:** `src/entities/extraction/extract.py` (enrichment trigger), downstream linking / saved-search task wiring
**Related:** [`../extraction.md`](../extraction.md), [`kgdb_event_persistence.md`](kgdb_event_persistence.md)

## Already shipped (essential-by-default)

The cheap-tier-by-default optimization is implemented and live:

- All 9 event schemas tag every field `"importance": "essential" | "secondary"` (essential =
  `event_type, name, description, context, status, date_range, location`). No required field is
  secondary, so essential-only records still validate against the full schema.
- `prompt_generator.py`: `essential_only=True` filters to essential fields (+ their composite
  types) and prunes `meta.example`; `generate_all_essential()` saved the `{supertype}_essn.txt`
  prompt for every event supertype.
- `extract.py`: `EntityExtractor(essential_prompts=True)` (the default) uses `{supertype}_essn.txt`
  via `_resolve_prompt_path()`, **falling back to the full prompt** when no `_essn` exists (themes/
  entities). The extraction cache key includes the variant (`essn`/`full`).

The essential set is exactly what the linker consumes (date/location/name/type drive the candidate
filter, `description` feeds adjudication, `status` is lifecycle), so the whole extraction→linking
pipeline runs essential-only with no loss.

## Remaining work — what triggers the full (enrichment) pass

Today the full prompt is reachable only as a **global** instance flag (`EntityExtractor(
essential_prompts=False)`). What's missing is the **selective, on-demand** enrichment: extract the
secondary fields **only for events that matter**, re-using the cached article text so the universal
essential pass stays cheap.

At first extraction we don't yet know an event's importance (e.g. multi-source-ness is only known
*after* linking). So frame it as **essential-always at extraction, enrich on demand**, with the
importance signal coming from downstream. Candidate signals:

- the `relevance` score (already extracted),
- post-linking `source_ids` count (multi-source ⇒ important),
- `event_type` priority,
- customer-search relevance (an event a saved search hits).

Cleanest shape: enrichment as a **separate task** over events that matter (mirrors the platform's
saved-search AI-task pattern), re-using the cached article text — so the extra call is spent only
where it pays off. The `importance="secondary"` tagging (done) is the prerequisite.

## Caveats

- Maintain two generated prompt variants per supertype (regeneration cost) — already the case.
- `meta.example` must stay consistent with the filtered field set.
- Secondary-absent records validate fine (no required field is secondary); the kgdb
  `entities.metadata` carries nulls for secondary fields until an event is enriched.

## Sequencing

A **pre-productionization** cost optimization, parallel to (not blocking) the linking-quality work.
Worth landing before the streaming write path in [`kgdb_event_persistence.md`](kgdb_event_persistence.md)
so we don't productionize the full-fat extraction cost.
