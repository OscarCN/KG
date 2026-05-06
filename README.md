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
  schema/           # Schema system for data normalization
    schemas/        # Pipeline schema definitions (JSON + Python)
    types/          # Type parsers, composite types, registry
    parse_object.py # Core Parser class
  entities/         # Entity extraction and linking
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
      link.py       # EntityLinker: candidate filter + LLM call (events only). Exposes link_one(raw) → LinkResult for streaming callers.
      run_linking.py# IPython runner: streams extracted_raw/*.json → linked/*.json + (optional) tags pipeline
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
cache/              # Extraction + linking + tags LLM-call cache (sha256-keyed, auto-generated)
```

### Schema System (`src/schema/`)

Declarative schema definitions in JSON with a Python normalization pipeline. See [`src/schema/readme_schema.md`](src/schema/readme_schema.md) for full documentation.

- Schemas defined in JSON with string type references, meta descriptions, and support for callable defaults/validators
- Reusable composite types (e.g. `DateRangeFromUnstructured`, `DateFromUnstructured`, `Location`, `PriceRange`, `CasualtyCount`) auto-resolved across schemas, including inside lists (e.g. `List[DateRangeFromUnstructured]`)
- Parser pipeline: structure mapping → type parsing → defaults → validation

### Entity Linking (`src/entities/linking/`)

Deduplicates and merges extracted **events** (the output of `src/entities/extraction/`) into canonical event records, each carrying a `source_ids` list of every document that mentions it. The flow:

1. **Geocode** the structured `Location` via deepriver's geocoder microservice to obtain `level_2_id` (state) and basic coords.
2. **Candidate filter** — events that share `event_type`, have date-range overlap, and same `level_2_id`.
3. **LLM disambiguation** — a single call to `google/gemini-2.5-flash-lite` (via OpenRouter) given the incoming event's `name`, `description`, structured address, and `date`, plus those same fields for each candidate. The LLM returns the matching candidate id or `null`.
4. **Merge or create** based on the LLM's answer.
5. *(Planned)* **Persist** the linked record into the unified `kgdb` Postgres database (canonical `entities` row + supertype/child `entity_types` + geocoded `entity_locations` + event lookup `event_properties` + per-source `entities_documents`). **Not implemented yet** — the linker's output is currently an in-memory / JSON record; the kgdb persistence model exists to guide architecture decisions while we iterate on linking approaches.

Both geocode and LLM responses are cached on disk (`cache/geocode/`, `cache/link_llm/`), keyed by sha256 of the canonical input — re-runs avoid re-billing. Themes and entities are not linked yet (skipped). See [`src/entities/linking/readme_linking.md`](src/entities/linking/readme_linking.md) for the linking pipeline and the [KG Database Persistence](src/entities/linking/readme_linking.md#kg-database-persistence) section for the (target) kgdb write model. Full kgdb schema and cross-database conventions live in [`media-backend-paid/docs/DATABASE_POSTGRES.md`](../../media-backend-paid/docs/DATABASE_POSTGRES.md).

The runner streams articles through the linker one at a time and (when `TAGS_ENABLED=True`, default) routes each newly-linked event into the [tags subsystem](src/entities/tags/readme_tags.md) for stance + claim extraction.

### Tags (`src/entities/tags/`)

Two complementary tag types extracted from articles, posts, and comments tied to a single **customer entity**:

- **Stances** — durable attitudes/qualities expressed about the customer (cross-event, per-customer catalog).
- **Claims** — specific factual assertions about events affecting the customer (per-event clusters with `is_new` freshness flag and `importance` score).

Five-phase pipeline: bootstrap (one-shot per customer) → tag (Phase 2 — single LLM call producing both stance assignments and structured claims) → adjudicate stance catalog mutations (Phase 3) → cluster claims (Phase 4) → apply (Phase 5). Stage 1 today is **in-memory only** — no Postgres writes; the snapshot file is a debug artefact. Stage 2 will swap `load_customer_from_json` for `load_customer_from_db` in `run_linking.py` and add a `Persistence` Postgres impl alongside `InMemoryPersistence`. See [`src/entities/tags/tags_overview.md`](src/entities/tags/tags_overview.md) for the design, [`src/entities/tags/tags_impl_plan.md`](src/entities/tags/tags_impl_plan.md) for the architecture, [`src/entities/tags/readme_tags.md`](src/entities/tags/readme_tags.md) for usage.

### Entity Extraction (`src/entities/extraction/`)

LLM-based structured extraction from unstructured text (news articles, social media). See [`src/entities/extraction/readme_extraction.md`](src/entities/extraction/readme_extraction.md) for full documentation, and [`src/entities/readme_entities.md`](src/entities/readme_entities.md) for the broader pipeline overview.

- **Ontology**: three-level hierarchy (matching rule → class → supertype → schema) with rules in Excel (`keywords.xlsx`) and type mapping in CSV (`event_types.csv`). Each schema declares its category (`event`, `theme`, or `entity`) via `meta.category`
- **16 supertypes** — 8 events + 7 themes + 1 entity/concept:
  - **Events** (identifiable single occurrences with location and datetime): paid_mass_event, robbery_assault_event (incl. kidnapping), public_works_event (incl. trash, sinkhole, public road, water-system works), violence_event, closures_interruptions_event, emergency_event (incl. pedestrian hit), protest_event, arrest_event
  - **Themes** (topical classifiers, no required datetime): security, public_infrastructure, civil_protection, mobility, culture, sports, civic_participation
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

The system implements **events** (8 supertypes — identifiable single occurrences with a location and date), **themes** (7 supertypes — topical classifiers without required datetime), and **entities/concepts** (1 supertype — `legislative_initiative`, with more planned). A theme matches whenever an article addresses, reports on, or touches any subject within its domain — whether through a specific event, a complaint, statistics, policy discussion, or a passing mention. An entity/concept matches only when the article refers to a specific, identifiable item of that type (with a proper name or distinguishing attributes), not a generic mention of the domain. An article may match a theme, an event, and an entity schema simultaneously — all are extracted separately. Events have `_event` suffix in their supertype name (e.g. `arrest_event`, `emergency_event`); themes and entities do not (e.g. `security`, `mobility`, `legislative_initiative`). Category is declared explicitly on each schema via `meta.category`.

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
