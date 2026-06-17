# TODO — Tiered extraction: essential vs secondary fields (token savings)

**Status:** in progress — schema tags + plumbing **done**; prompt generation + on-demand enrichment trigger **open**
**Area:** `src/entities/extraction/schemas/*.json`, `src/entities/extraction/prompt_generator.py`, `src/entities/extraction/extract.py`
**Related:** [`../../src/entities/extraction/readme_extraction.md`](../../src/entities/extraction/readme_extraction.md), [`kgdb_event_persistence.md`](kgdb_event_persistence.md)

## Implemented so far

- All 9 event schemas tag every field `"importance": "essential" | "secondary"` (essential =
  `event_type, name, description, context, status, date_range, location`). No required field is
  secondary, so essential-only records still validate against the full schema.
- `prompt_generator.py`: `PromptGenerationContextManager(supertype, essential_only=True)` filters
  to essential fields (+ their composite types) and prunes `meta.example`; `generate(...,
  essential_only=True)` saves `{supertype}_essn.txt`; `generate_all_essential()` does it for every
  supertype with a secondary field (events only).
- `extract.py`: `EntityExtractor(essential_prompts=True)` (the default) prefers `{supertype}_essn.txt`
  via `_resolve_prompt_path()`, **falling back to the full prompt** when no `_essn` exists (themes/
  entities, or before generation). Extraction cache key includes the variant (`essn`/`full`;
  `full` = no suffix, backward compatible).

**Still open:** generate the `_essn` prompts (`PromptGeneration().generate_all_essential()` — LLM
cost), and design the **trigger** for the on-demand full (enrichment) pass (below).

## Idea

Most extraction calls only need the fields that **identify** an event; the rest is enrichment we
rarely use. Split each schema's attributes into two tiers and extract the cheap tier by default,
the rich tier only on demand — saving LLM tokens (input *and* output) across the bulk of events.

- Add `"importance": "essential" | "secondary"` to **each attribute** in the schema JSON.
  - **Essential:** identification — `date` (`date_range`), `location`, `name`, `type`
    (`event_type`/`event_subtype`) — plus `description`, `context`, `status` (planned / completed
    / cancelled / …).
  - **Secondary:** everything else (e.g. `price`, `attendance`, `venue_capacity`, `organizer`,
    `performer`; per-supertype enrichment fields).
- `prompt_generator.py` filters attributes by tier and generates **two prompts per supertype**:
  an *essential* prompt (the default, used in most cases) and a *full* prompt.
- `extract.py` runs the **essential** prompt by default; the **full** prompt only for events
  judged important (trigger below).

## Why it fits

- The **essential set is exactly what the linker consumes** — date/location/name/type drive the
  candidate filter, `description` feeds LLM adjudication, `status` is lifecycle. Secondary fields
  are report/display enrichment, **not** linking inputs. So essential-only extraction runs the
  whole extraction→linking pipeline with no loss; enrichment is genuinely optional.
- Rides the existing schema-driven prompt generation: prompts are already built from per-field
  `description`s + composite-type descriptions, so tiering is a pre-generation filter.
- Saves **output** tokens (smaller schema — output dominates cost) **and input** tokens (shorter
  prompt). Likely improves essential-field accuracy (less for the model to juggle; fewer
  hallucinated secondary fields).

## Key decision — what triggers the full (enrichment) pass

At first extraction we don't yet know an event's importance (e.g. multi-source-ness is only known
*after* linking). Frame it as **essential-always at extraction, enrich on demand**, with the
importance signal coming from downstream. Candidate signals:
- the `relevance` score (already extracted),
- post-linking `source_ids` count (multi-source ⇒ important),
- `event_type` priority,
- customer-search relevance (an event a saved search hits).

Cleanest shape: enrichment as a **separate task** over events that matter (mirrors the platform's
saved-search AI-task pattern), re-using the cached article text — so the essential pass stays
cheap and universal and the extra call is spent only where it pays off. The
`importance="secondary"` tagging is the prerequisite either way.

## Caveats

- Maintain two generated prompt variants per supertype (regeneration cost).
- `meta.example` must stay consistent with the filtered field set (the README already requires
  examples to include all subfields of composite types).
- Validation already treats most fields as nullable, so secondary-absent records validate fine —
  confirm per schema. The kgdb `entities.metadata` would carry nulls for secondary fields until
  an event is enriched.

## Sequencing

A **pre-productionization** cost optimization, parallel to (not blocking) the linking-quality
work. Worth landing before the streaming write path in [`kgdb_event_persistence.md`](kgdb_event_persistence.md)
so we don't productionize the full-fat extraction cost.
