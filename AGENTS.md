# AGENTS.md

Guidance for coding agents working in this repository.

## Project Snapshot

This repo implements a Spanish-language, Mexico-focused knowledge graph pipeline for:

- extracting structured entities from unstructured / semi-structured content,
- linking extracted events into canonical KG records,
- tagging linked customer-relevant content with customer-anchored stances and per-event claim clusters.

The main active code lives under `src/entities/`. Legacy proof-of-concept scripts live under `src/PoC/`.

## Read First

Before changing entity extraction, linking, or tags code, read the relevant docs:

- Root overview: `README.md`
- Project conventions: `CLAUDE.md`
- Entity overview: `src/entities/readme_entities.md`
- Extraction: `src/entities/extraction/readme_extraction.md`
- Linking: `src/entities/linking/readme_linking.md`
- Tags design: `src/entities/tags/tags_overview.md`
- Tags usage: `src/entities/tags/readme_tags.md`
- Tags Stage-1 architecture: `src/entities/tags/tags_impl_plan.md`

`README.mda` is not present in this checkout; use `README.md`.

## Architecture Boundaries

### Schema System

The reusable schema layer is under `src/schema/`.

- Schemas are JSON files loaded through `load_schema()`.
- Type parsing, defaults, and validation are handled by the shared `Parser`.
- Composite types live in `src/schema/types/composite_types.json`.
- Do not create one-off schema parsing logic inside extraction/linking/tags when the shared schema system can do it.

### Entity Extraction

Extraction lives in `src/entities/extraction/`.

- Routing is `keyword -> class -> supertype -> schema`.
- Ontology catalogues live in `catalogues/event_types.csv` and `catalogues/keywords.xlsx`.
- Every supertype schema must declare `meta.category` as `event`, `theme`, or `entity`.
- Extraction prompts in `prompts/classes/` are generated from schemas by `prompt_generator.py`.
- Prompt quality depends heavily on `meta.description`, field `description`, and complete `meta.example` structures.
- Composite examples must include every composite subfield, using `null` for absent values.

Extraction output is a flat list of validated records tagged with `_source_id` and `_supertype`.

### Entity Linking

Linking lives in `src/entities/linking/`.

- Current linker scope is events only.
- Themes and entities/concepts are skipped for now and counted in `linker.dropped`.
- `EntityLinker.link_one(raw)` is the streaming entry point used by `run_linking.py`.
- Candidate filtering uses event type, date overlap, and geocoded `level_2_id`.
- LLM disambiguation happens in `link_llm.py` and is cached under `cache/link_llm/`.
- Geocoding happens through `geocode.py` and is cached under `cache/geocode/`.

The linker currently writes in-memory / JSON output only. The kgdb persistence model in the docs is target architecture, not implemented behavior.

### Tags

Tags live in `src/entities/tags/`.

The Stage-1 tags implementation described in `tags_impl_plan.md` has been implemented in this folder. Treat it as the current baseline, not a future TODO.

Core design constraints:

- Tags are parameterized by one customer entity.
- Stances are customer-anchored, durable qualities or attitudes.
- Stance catalogs are per customer and should remain cross-event.
- Claims are specific factual assertions that affect the customer.
- Claim catalogs are scoped per `(customer, event)` and composed of clusters.
- A source item may have one stance and zero or more claims.
- Claim retention is strict: keep only claims whose `affected_entity_ids` include the customer.
- Stage 1 is in-memory only. Snapshot JSON files are inspection/debug artifacts, not database writes.
- Stage 2 Postgres persistence is still future work.

The streaming integration is driven by `src/entities/linking/run_linking.py`: extracted records are linked article by article, and linked events are passed into the tags pipeline when `TAGS_ENABLED = True`.

### Tags GPT

`src/entities/tags_gpt/` is the decoupled experimental implementation for the next tags iteration. Prefer it when working on the new implementation. It keeps each step separate and injectable: extraction-output adapter, content retrieval, event-candidate retrieval, event linking, stance tagging, stance updating, claim tagging, and claim updating. It is not wired into `run_linking.py` by default.

## Current Data / Output Conventions

- Extraction input examples live under `data/<subdir>/`.
- Raw extraction outputs live under `data/extracted_raw/`.
- Linked event outputs live under `data/linked/`.
- Tag snapshots live under `data/tags/<customer_slug>/run_<ts>.json`.
- Customer fixtures are `data/tags/customer_<entity_id>.json`.
- LLM and retrieval caches live under `cache/`.

Do not treat cache files or Stage-1 tag snapshots as durable source of truth.

## Environment And Services

Most end-to-end scripts require external services and credentials:

- `OPENROUTER_API_KEY` for extraction, linking, prompt generation, and tags LLM calls.
- Geocoder URLs: `NLP_URL`, `GEOCODING_URL`.
- Elasticsearch credentials when not using local fixture retrieval.
- kgdb credentials only for fixture generation or future DB-backed work.

There is no `requirements.txt` or `pyproject.toml` yet. Dependencies are currently installed manually.

## Implementation Rules

- Prefer existing patterns over new abstractions.
- Keep changes scoped to the subsystem being modified.
- Preserve the distinction between implemented behavior and documented future design.
- Do not add Postgres writes unless the task explicitly asks for Stage-2 persistence.
- Do not make themes/entities flow through the event linker unless the task is specifically to implement that.
- Keep LLM calls cacheable with stable canonical payloads and sha256 keys.
- Keep LLM outputs JSON-shaped and defensively parsed.
- Keep model choices configurable through environment variables.
- For Spanish ontology prompts and schema descriptions, maintain Spanish-language output instructions.
- When adding or changing schema fields, update examples and regenerate prompts when needed.

## Documentation Rules

After any non-trivial implementation or behavioral change, update the relevant docs in the same change:

- Root or cross-system behavior: `README.md`
- Entity-wide behavior: `src/entities/readme_entities.md`
- Extraction changes: `src/entities/extraction/readme_extraction.md`
- Linking changes: `src/entities/linking/readme_linking.md`
- Tags changes: `src/entities/tags/readme_tags.md`, `tags_overview.md`, or `tags_impl_plan.md`
- Tags GPT changes: `src/entities/tags_gpt/readme_tags_gpt.md`

Keep docs lean and consistent. Remove or revise stale claims rather than adding contradictory notes.

## Testing / Verification

Use the smallest verification that exercises the changed behavior.

Useful entry points:

```bash
ipython src/PoC/run_extraction.py
ipython src/entities/linking/run_linking.py
python scripts/build_customer_fixture.py 75
```

Only run end-to-end LLM/geocoder/ES workflows when credentials and services are available, and expect cache hits to affect repeat runs.

## Known Non-Goals Unless Requested

- Do not introduce a package manager or dependency manifest casually.
- Do not migrate legacy `src/PoC/` code unless asked.
- Do not implement claim fact verification; current design flags novelty and importance only.
- Do not implement cross-event claim linking unless explicitly assigned.
- Do not implement multi-stance per source item unless explicitly assigned.
- Do not persist tags or linked entities to Postgres unless explicitly assigned.
