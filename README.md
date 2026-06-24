# Knowledge Graph — Entity Linking System

Entity linking system that matches entities found in unstructured and semi-structured sources (news articles, social media, websites, contracts, databases) to ground truth entities in a knowledge base.

## System Components

### Knowledge Base (KB)

Two knowledge bases:

- **Geographic KB** — Hierarchical geographic entities (countries, provinces, cities, neighborhoods, streets, places) linked by "is in" relations. Each entity has coordinates, shape, and aliases.
- **Entities/Events KB** — Entities, concepts, themes, and events typed by an ontology. See [Ontology categories](#ontology-categories) below for the distinction between these types. Each entry has attributes defined by its ontology class schema.

Each entity has an **ontology class** that defines its schema (attributes, identifying features) and how it should be uniquely described. Ontology schemas are defined in JSON and parsed by the schema system.

### Retrieval

Multiple retrieval strategies depending on entity type:
- **Name similarity** — Locality-sensitive hashing (LSH) via Redis for efficient fuzzy name matching
- **Geographic** — Coordinate-based queries (point-in-shape, nearest) via PostgreSQL
- **Semantic** — Embedding-based similarity on descriptions via vector database

### Disambiguation / Linking

Deciding the correct entity from a set of candidates using features derived from:
- Language (descriptions, narratives)
- Location (coordinates, addresses)
- Time, taxonomies, identifiers

## Repository Structure

```
src/
  listener.py       # Streaming RabbitMQ consumer: documents → extract → link → persist (kgdb)
  schema/           # Schema system for data normalization
    schemas/        # Pipeline schema definitions (JSON + Python)
    types/          # Type parsers, composite types, registry
    parse_object.py # Core Parser class
  entities/         # Entity extraction and linking
    run_entities.py # Integration runner: streams documents through extraction, then linking
    document.py     # record_to_article: map a raw document envelope to the extractor's input (shared)
    extraction/     # LLM-based structured extraction from text
      schemas/      # Entity schemas (one per supertype, JSON)
      catalogues/   # Ontology catalogues (event types CSV, keywords Excel)
      prompts/classes/ # Generated LLM extraction prompts (one per supertype)
      extract.py    # Extraction pipeline
      prompt_generator.py # Schema → LLM prompt auto-generation
      readme_extraction.md # Extraction subsystem docs
    linking/        # Event linking/deduplication and KG database persistence
      geocode.py    # Thin client for deepriver's geocoder microservice (structured-input)
      link_llm.py   # LLM disambiguator (gemini-2.5-flash-lite) with file cache
      index.py      # CandidateIndex + RecordStore protocols + in-memory implementations
      kgdb_retrieval.py # kgdb-backed CandidateIndex (SQL column-reconstruction) + RecordStore (reads entities.metadata)
      mx_states.py  # Static Mexican-state catalogue (geo partition-key normalization + fallback)
      strategy.py   # GeoEventStrategy: per-supertype identification lifecycle (enrich → keys → adjudicate → merge)
      link.py       # EntityLinker: envelope parse + strategy orchestration (events only). Exposes link_one(raw) → LinkResult for streaming callers.
      persistence.py# KgdbWriter: idempotent write of a linked record into kgdb (Step Zero batch/stream writer)
      run_linking.py# IPython runner: tests linking from extracted_raw/*.json fixtures → linked/*.json
      readme_linking.md # Linking subsystem docs (incl. KG database persistence)
    tags/           # Customer-anchored stances + per-event claim clusters (Stage 1, in-memory)
      models/       # Pure data structures: customer.py, source_item.py, stance_catalog.py, claim_catalog.py
      bootstrap.py / tagging.py / stance_adjudicator.py / claim_clusterer.py / apply.py
      retrieval.py / persistence.py / stats.py
      prompts/      # Spanish prompts for the four LLM phases
      tags_overview.md / tags_impl_plan.md / readme_tags.md
    readme_entities.md # Overview, ontology categories, links to subsystem docs
  llm/              # LLM provider clients
    openrouter/     # OpenRouter API client (OpenAI-compatible)
  PoC/              # Proof-of-concept implementations (legacy)
    events.py       # Event extraction from news via GPT-4o (batch + sync)
    event_linking.py# Event deduplication and merging across sources
    newsfeed.py     # News relevance classification and structured extraction
    get_data.py     # Fetch ES hits via elastic_client → data/<subdir>/*.json
    get_entities_data.py # Build an incoming-document fixture from one keywords.xlsx row
    get_geo_event_fixtures.py # Build per-supertype fixtures (all rules for a supertype) scoped to a state (locations_mentioned.level_2_id) + date window
    run_extraction.py# Step-by-step IPython script for the extraction pipeline
    sentence_pairs_model.py  # Sentence-pair similarity model (PyTorch)
resources/          # Input data files (Excel, prompt contexts)
data/
  extracted_raw/    # Output of the extraction pipeline
  linked/           # Output of the linking pipeline
  tags/
    customer_<entity_id>.json   # Stage-1 customer fixture (mirrors kgdb columns)
    customer_<entity_id>/run_<ts>.json  # Per-run snapshot of the stance + claim catalogs
scripts/
  build_customer_fixture.py  # Materialises a customer fixture from kgdb (Stage-1 stand-in)
  gen_kg_catalog_seed.py     # Generates the kgdb KG type-catalog seed SQL (P2) from schemas + event_types.csv
  persist_linked.py          # Step Zero: writes a data/linked/<stem>.json fixture into kgdb via KgdbWriter
  enqueue_from_es.py         # Testing producer: ES date-window fetch (geo/precision/category filter) → kg RabbitMQ doc queue
  publish_document.py        # Publish a JSON document file to a RabbitMQ queue (dev-vhost listener loop)
cache/              # Extraction + linking LLM-call cache (sha256-keyed, auto-generated)
docs/
  todos/            # Roadmap / design TODOs — one self-contained file per TODO
```

### Schema System (`src/schema/`)

Declarative schema definitions in JSON with a Python normalization pipeline. See [`src/schema/readme_schema.md`](src/schema/readme_schema.md) for full documentation.

- Schemas defined in JSON with string type references, meta descriptions, and support for callable defaults/validators
- Reusable composite types (e.g. `DateRangeFromUnstructured`, `DateFromUnstructured`, `Location`, `PriceRange`, `CasualtyCount`) auto-resolved across schemas, including inside lists (e.g. `List[DateRangeFromUnstructured]`)
- Parser pipeline: structure mapping → type parsing → defaults → validation

### Entity Linking (`src/entities/linking/`)

Deduplicates and merges extracted **events** (the output of `src/entities/extraction/`) into canonical event records, each carrying a `source_ids` list of every document that mentions it. The supertype-specific behaviour lives in a strategy object (`GeoEventStrategy`) behind a `CandidateIndex` protocol; `EntityLinker` only parses the envelope and orchestrates. The flow:

1. **Geocode** the structured `Location` via deepriver's geocoder microservice; the state (`level_2`, normalized through a static Mexican-state catalogue, with the extracted `location.state` text as deterministic fallback) becomes the geo partition key.
2. **Candidate filter** — events that share `event_type`, the same geo partition (located lookups also probe the no-location bucket), and date-range overlap (slack widened by the extraction's `precision_days` on approximate dates; publication-date fallback at ±2 days).
3. **LLM disambiguation** — a single call to `google/gemini-2.5-flash-lite` (via OpenRouter) given the incoming event's `name`, `description`, structured address, and `date`, plus those same fields for each candidate (capped, most recent first). The LLM returns the matching candidate id or `null`.
4. **Merge or create** based on the LLM's answer. Merges keep the most precise extracted date window as the canonical range (per-source windows are tracked on the record) instead of widening unconditionally.
5. **Persist** the linked record into the unified `kgdb` Postgres database (canonical `entities` row + supertype/child `entity_types` + geocoded `entity_locations` + event lookup `event_properties` + per-source `entities_documents`). **Step Zero implemented** — `linking/persistence.py` (`KgdbWriter`) does an idempotent, one-transaction-per-record write; `scripts/persist_linked.py` drives it from a `data/linked/<stem>.json` fixture (validated on dev). The streaming RabbitMQ consumer and in-DB merge remain pending. Persistence is a decoupled step; the linker's own output is still the in-memory / JSON record.

Both geocode and LLM responses are cached on disk (`cache/geocode/`, `cache/link_llm/`), keyed by sha256 of the canonical input — re-runs avoid re-billing. Themes and entities are not linked yet (skipped). See [`src/entities/linking/readme_linking.md`](src/entities/linking/readme_linking.md) for the linking pipeline and the [KG Database Persistence](src/entities/linking/readme_linking.md#kg-database-persistence) section for the (target) kgdb write model. Full kgdb schema and cross-database conventions live in [`media-backend-paid/docs/DATABASE_POSTGRES.md`](../../media-backend-paid/docs/DATABASE_POSTGRES.md).

The linker runner is a local test harness for linking after extraction: it reads an extracted-record fixture from `data/extracted_raw/`, streams records through `EntityLinker.link_one(raw)`, and writes linked canonical events to `data/linked/`. It does not fetch article/comment content or run tags.

For an end-to-end local simulation of the production shape, use `src/entities/run_entities.py`: it reads incoming document fixtures from `data/<subdir>/`, processes one document at a time through `EntityExtractor.extract(article)`, immediately streams each extracted record through `EntityLinker.link_one(raw)`, and writes debug artifacts to `data/extracted_raw/` and `data/linked/`.

### Tags (`src/entities/tags/`)

The tags implementation is decoupled from extraction/linking and is moving to its own repository. The copy in this repository should not be wired into `src/entities/linking/run_linking.py`.

Two complementary tag types extracted from articles, posts, and comments tied to a single **customer entity**:

- **Stances** — durable attitudes/qualities expressed about the customer (cross-event, per-customer catalog).
- **Claims** — specific factual assertions about events affecting the customer (per-event clusters with `is_new` freshness flag and `importance` score).

Five-phase pipeline: bootstrap (one-shot per customer) → tag (Phase 2 — single LLM call producing both stance assignments and structured claims) → adjudicate stance catalog mutations (Phase 3) → cluster claims (Phase 4) → apply (Phase 5). Two run modes coexist: (a) the in-memory IPython driver `run_tags.py` with JSON snapshots, and (b) the userdb-backed streaming script `stream.py` — file-simulated today but RabbitMQ-shaped, writing every mutation to userdb per the schema in [`src/entities/tags/serialization_plan.md`](src/entities/tags/serialization_plan.md) (folded into [`media-backend-paid/db/user_db/schema.sql`](../../media-backend-paid/db/user_db/schema.sql)). See [`src/entities/tags/tags_overview.md`](src/entities/tags/tags_overview.md) for the design, [`src/entities/tags/tags_impl_plan.md`](src/entities/tags/tags_impl_plan.md) for the architecture, [`src/entities/tags/readme_tags.md`](src/entities/tags/readme_tags.md) for usage.

### Entity Extraction (`src/entities/extraction/`)

LLM-based structured extraction from unstructured text (news articles, social media). See [`src/entities/extraction/readme_extraction.md`](src/entities/extraction/readme_extraction.md) for full documentation, and [`src/entities/readme_entities.md`](src/entities/readme_entities.md) for the broader pipeline overview.

- **Ontology**: three-level hierarchy (matching rule → class → supertype → schema) with rules in Excel (`keywords.xlsx`) and type mapping in CSV (`event_types.csv`). Each schema declares its category (`event`, `theme`, or `entity`) via `meta.category`
- **16 supertypes** — 9 events + 6 themes + 1 entity/concept:
  - **Events** (identifiable single occurrences with location and datetime): paid_mass_event, robbery_assault_event (incl. kidnapping), public_works_event (incl. trash, sinkhole, public road, water-system works), public_infrastructure_event (broader civic events around infrastructure — complaint waves, policy announcements, planning decisions, general water-supply issues), violence_event, closures_interruptions_event, emergency_event (incl. pedestrian hit), protest_event, arrest_event
  - **Themes** (topical classifiers, no required datetime): security, civil_protection, mobility, culture, sports, civic_participation
  - **Entities/Concepts** (specific, identifiable things that are not events): legislative_initiative (incl. law initiative, reform, decree, regulation, legislative agreement, ratification)
- **Three-step extraction flow**: keyword matching → LLM classification (filters candidate classes to those actually discussed, with per-category selection criteria for events, themes, and entities/concepts) → per-class LLM extraction (one call per confirmed class, scoped to that class)
- **Schema-driven prompt generation**: extraction prompts auto-generated from JSON schemas via LLM using a generate+feedback loop (`prompt_generator.py`). Each prompt is built from three context layers — class `meta.description`, field `description`, and composite type descriptions (e.g. `DateRangeFromUnstructured` contributes approximate-date and `precision_days` instructions). `paid_mass_event.txt` serves as the style exemplar. Write schema descriptions carefully — they directly affect prompt quality. `meta.example` must include ALL subfields of composite types (with null for absent values) — omitting fields causes generated prompts to miss them.
- **Same schema infrastructure**: entity schemas use the same JSON format, `load_schema()`, `Parser`, and composite types as pipeline schemas

### Ontology Categories

The system classifies content into three broad ontology categories, each with different identifying characteristics:

| Category | Description | Identifying features | Examples |
|----------|-------------|---------------------|----------|
| **Event** | A specific, identifiable occurrence that happened at a particular time and place | Location + date/time make each event distinguishable from others | A concert, an accident, a protest, an arrest |
| **Theme** | A topical classification — any article that touches or discusses a related subject matches | Optional location (city/state level), no required date — acts as a broad classifier for article content | Security (crime, violence, policing), mobility (traffic, transit), culture (arts, heritage) |
| **Entity/Concept** | A specific, identifiable thing that is not an event | May have a name, location, or other identifying attributes, but not necessarily a date | A real estate development, a specific technology, a chemical compound, an individual person, a law initiative |

The system implements **events** (9 supertypes — identifiable single occurrences with a location and date), **themes** (6 supertypes — topical classifiers without required datetime), and **entities/concepts** (1 supertype — `legislative_initiative`, with more planned). A theme matches whenever an article addresses, reports on, or touches any subject within its domain — whether through a specific event, a complaint, statistics, policy discussion, or a passing mention. An entity/concept matches only when the article refers to a specific, identifiable item of that type (with a proper name or distinguishing attributes), not a generic mention of the domain. An article may match a theme, an event, and an entity schema simultaneously — all are extracted separately. Events have `_event` suffix in their supertype name (e.g. `arrest_event`, `emergency_event`); themes and entities do not (e.g. `security`, `mobility`, `legislative_initiative`). Category is declared explicitly on each schema via `meta.category`.

The extraction pipeline (keyword matching → LLM classification → per-class extraction) is designed to work with all three categories. The classification prompt presents candidates in up to three groups (Eventos, Temas, Entidades/Conceptos) with their own selection criteria, and per-class extraction runs the schema bound to the confirmed class's supertype.

### Future: Class Inheritance

Classes will support inheritance, where a more specific class inherits attributes from a broader one. The current event/theme naming convention supports this:
- **violence_event** inherits from **security** (theme) — a specific shooting inherits the general security topic attributes
- **public_works_event** inherits from **public_infrastructure** (theme)
- **emergency_event** inherits from **civil_protection** (theme)
- **closures_interruptions_event** inherits from **mobility** (theme)
- **protest_event** inherits from **civic_participation** (theme)
- **water_usage_law** (entity) inherits from **legislative_initiative** (entity) — a specific water regulation inherits general legislative initiative attributes

This allows shared attributes and behavior to be defined once at the parent level and specialized at the child level.

A related future direction is **multi-class entities** — a single entity instantiating more than one ontology class simultaneously (e.g. an `arrest_event` that is also a `violence_event`, or an entity acting as a theme anchor at the same time). The `entity_types` table on the kgdb side is already a many-to-many and supports it on the schema side. The open questions are at the validation layer (which class's schema does `entities.metadata` conform to when an entity carries several?) and at the linker (does multi-class membership widen the candidate filter?). We'll address it alongside inheritance — until then, the working assumption is one supertype per entity.

## Infrastructure

| Service | Purpose |
|---------|---------|
| PostgreSQL | Knowledge base storage, geographic queries |
| Redis | LSH cache for fuzzy name matching |
| Elasticsearch | News article indexing and retrieval |
| MongoDB | Crawler/source metadata |
| OpenRouter | LLM calls for extraction (`OPENROUTER_MODEL`), prompt generation (`OPENROUTER_GENERATION_MODEL`), prompt feedback (`OPENROUTER_FEEDBACK_MODEL`), event linking (`OPENROUTER_LINKER_MODEL`), tags bootstrap / tagger / adjudicator / clusterer (`OPENROUTER_BOOTSTRAP_MODEL`, `OPENROUTER_TAGGER_MODEL`, `OPENROUTER_ADJUDICATOR_MODEL`, `OPENROUTER_CLUSTERER_MODEL`) |
| OpenAI API | Embeddings |

## Key Dependencies

`openai`, `requests`, `torch`, `tensorflow_hub`, `sklearn`, `pandas`, `numpy`, `psycopg2`, `pymongo`, `elasticsearch`, `redis`, `dateutil`, `tldextract`, `matplotlib`

## Roadmap / TODOs

Design and roadmap TODOs live in [`docs/todos/`](docs/todos/) — **one self-contained file per TODO**.

- [Per-supertype retrieval & linking strategy](docs/todos/retrieval_linking_per_supertype.md) — make candidate retrieval and linking a declarable per-supertype (and per class-family) strategy of attributes + methods. The **geo strategy v1** spec (identity model, date/geo fallback tiers, precision fixes) is **implemented** (`src/entities/linking/strategy.py`); the per-supertype generalization and the `person_actions` strategy remain open.
- [Tighten `robbery_assault_event` matching keywords](docs/todos/robbery_assault_keywords.md) — the `security_event` class matches thematic security terms (and the bare `Seguridad` category), polluting the linking partition with non-incidents and overlapping the `security` theme. Narrow the keywords to discrete crime incidents.
- [Extract a list of locations, not a single one](docs/todos/location_level_list_extraction.md) — model `locations: List[Location]` so multi-street/venue events (e.g. "calles San Juan del Río y Amealco") geocode to several level-6/7 places, letting two records that share a street merge deterministically. Fixes the coarse-precision under-merge (the El Marqués case).
- [Canonical↔canonical reconciliation (consistency pass & multi-match merge)](docs/todos/canonical_reconciliation.md) — the linker only merges an incoming record into one existing event and never reconciles two already-canonical events, so a misdated/coarse seed forks a permanent twin (the Zona Fest `festival` 586469 / 445112 pair at Estadio Corregidora). Explore a multi-match merge at link time and/or a periodic consistency sweep, both built on a canonical-merge primitive + index re-pointing.
- [Retrieval idea: hard date+location, soft name+type](docs/todos/retrieval_name_soft_type.md) — invert the candidate filter: make date overlap + hierarchical geo-compatibility the hard gates, demote exact `event_type` to a soft signal, and add name-similarity retrieval (trigram first, LSH later). **Partially implemented:** soft type (`partition_on="supertype"`) and the hard hierarchical geo gate (`hard_geo_gate=True`) are live in `strategy.py`; name-similarity retrieval and the multi-match collapse it feeds remain open. Retrieves the cross-type/cross-precision variants of one event (the Zona Fest case) and produces the multi-match candidate set the reconciliation pass acts on.
- [Document retrieval strategy (keyword pre-filter → kg doc queue)](docs/todos/document_retrieval_strategy.md) — the **producer** side the streaming listener is missing. Decided **global ontology KG** (all news matching the ontology keywords, independent of saved searches). **MVP: stream the post-`gp3` enriched firehose into the kg doc queue and let the listener's `Ontology.match` pre-filter in-worker** (no LLM in matching; LLM cost is identical to pre-filtering, so it's near-free to adopt). An ES-query retriever (compile active `keywords.xlsx` → ES `bool` query via `elastic_client`; `get_entities_data.py` prototypes it) is deferred as a volume optimization and the home for historical backfill.
- [kgdb-backed candidate retrieval (durable CandidateIndex + record store)](docs/todos/kgdb_candidate_index.md) — **implemented (column reconstruction).** Candidate lookup + id→record resolution read from kgdb (`kgdb_retrieval.py`) instead of per-worker in-memory state, so the streaming listener dedups across restarts / multiple workers — validated cross-process (a second worker merges into the first's events instead of duplicating). `index.py` defines the full backend contract (`CandidateIndex` + `RecordStore`); the in-memory path is unchanged.
- [Classify & extract only active types](docs/todos/active_type_extraction.md) — gate classification/extraction to types flagged **active**, with the `active` flag wired into the kgdb type catalog (`entity_types_kinds_available`) as source of truth, elevating today's Excel-only `enabled` gate. Couples to the persistence catalog seed (P2).
- [Tiered extraction: on-demand enrichment pass](docs/todos/tiered_extraction_essential_fields.md) — **essential-by-default extraction is shipped** (schema `importance` tags, `_essn` prompts, default essential extraction with full fallback). Remaining: the **on-demand enrichment trigger** that extracts secondary fields only for events that matter (multi-source / high-relevance / saved-search hits), as a separate task re-using cached article text. A pre-productionization cost optimization, parallel to the linking work.
- [Persist linked events into kgdb (Step Zero → streaming write/merge)](docs/todos/kgdb_event_persistence.md) — write linked records into the unified kgdb Postgres DB; eventual end-state is a RabbitMQ consumer that writes/merges continuously (events + entities). **Step Zero done:** the decoupled batch writer (`linking/persistence.py` `KgdbWriter` + `scripts/persist_linked.py`) and its prerequisites (P1 `entity_locations` identity fix, P2 type-catalog seed) are implemented and validated on dev. **Remaining:** the streaming RabbitMQ consumer and the in-DB merge, which still benefit from the linking-quality work — *near-term order:* [multi-venue/multi-street locations](docs/todos/location_level_list_extraction.md) → [soft name/type retrieval + multi-match](docs/todos/retrieval_name_soft_type.md) → [canonical reconciliation](docs/todos/canonical_reconciliation.md).
