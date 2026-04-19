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
  entities/         # Entity extraction and mapping
    extraction/     # LLM-based structured extraction from text
      schemas/      # Entity schemas (one per supertype, JSON)
      catalogues/   # Ontology catalogues (event types CSV, keywords Excel)
      prompts/classes/ # Generated LLM extraction prompts (one per supertype)
      extract.py    # Extraction pipeline
      prompt_generator.py # Schema → LLM prompt auto-generation
    linking/        # (future) Entity linking/deduplication
  llm/              # LLM provider clients
    openrouter/     # OpenRouter API client (OpenAI-compatible)
  PoC/              # Proof-of-concept implementations (legacy)
    events.py       # Event extraction from news via GPT-4o (batch + sync)
    event_linking.py# Event deduplication and merging across sources
    newsfeed.py     # News relevance classification and structured extraction
    run_extraction.py# Step-by-step IPython script for the extraction pipeline
    sentence_pairs_model.py  # Sentence-pair similarity model (PyTorch)
resources/          # Input data files (Excel, prompt contexts)
cache/              # Extraction result cache (per article+class, auto-generated)
```

### Schema System (`src/schema/`)

Declarative schema definitions in JSON with a Python normalization pipeline. See [`src/schema/readme_schema.md`](src/schema/readme_schema.md) for full documentation.

- Schemas defined in JSON with string type references, meta descriptions, and support for callable defaults/validators
- Reusable composite types (e.g. `DateRangeFromUnstructured`, `DateFromUnstructured`, `Location`, `PriceRange`, `CasualtyCount`) auto-resolved across schemas, including inside lists (e.g. `List[DateRangeFromUnstructured]`)
- Parser pipeline: structure mapping → type parsing → defaults → validation

### Entity Extraction (`src/entities/`)

LLM-based structured extraction from unstructured text (news articles, social media). See [`src/entities/readme_entities.md`](src/entities/readme_entities.md) for full documentation.

- **Ontology**: three-level hierarchy (matching rule → class → supertype → schema) with rules in Excel (`keywords.xlsx`) and type mapping in CSV (`event_types.csv`). Each schema declares its category (`event`, `theme`, or `entity`) via `meta.category`
- **16 supertypes** — 8 events + 7 themes + 1 entity/concept:
  - **Events** (identifiable single occurrences with location and datetime): paid_mass_event, robbery_assault_event (incl. kidnapping), public_works_event (incl. trash, water, sinkhole, public road), violence_event, closures_interruptions_event, emergency_event (incl. pedestrian hit), protest_event, arrest_event
  - **Themes** (topical classifiers, no required datetime): security, public_infrastructure, civil_protection, mobility, culture, sports, civic_participation
  - **Entities/Concepts** (specific, identifiable things that are not events): legislative_initiative (incl. law initiative, reform, decree, regulation, legislative agreement, ratification)
- **Three-step extraction flow**: keyword matching → LLM classification (filters candidate classes to those actually discussed, with per-category selection criteria for events, themes, and entities/concepts) → per-class LLM extraction (one call per confirmed class, scoped to that class)
- **Schema-driven prompt generation**: extraction prompts auto-generated from JSON schemas via LLM using a generate+feedback loop (`prompt_generator.py`). Each prompt is built from three context layers — class `meta.description`, field `description`, and composite type descriptions (e.g. `DateRangeFromUnstructured` contributes approximate-date and `precision_days` instructions). `paid_mass_event.txt` serves as the style exemplar. Write schema descriptions carefully — they directly affect prompt quality. `meta.example` must include ALL subfields of composite types (with null for absent values) — omitting fields causes generated prompts to miss them.
- **Same schema infrastructure**: entity schemas use the same JSON format, `load_schema()`, `Parser`, and composite types as pipeline schemas

### Ontology Categories

The system classifies content into four broad ontology categories, each with different identifying characteristics:

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

## Infrastructure

| Service | Purpose |
|---------|---------|
| PostgreSQL | Knowledge base storage, geographic queries |
| Redis | LSH cache for fuzzy name matching |
| Elasticsearch | News article indexing and retrieval |
| MongoDB | Crawler/source metadata |
| OpenRouter | LLM calls for extraction (`OPENROUTER_MODEL`), prompt generation (`OPENROUTER_GENERATION_MODEL`), and prompt feedback (`OPENROUTER_FEEDBACK_MODEL`) |
| OpenAI API | Embeddings |

## Key Dependencies

`openai`, `requests`, `torch`, `tensorflow_hub`, `sklearn`, `pandas`, `numpy`, `psycopg2`, `pymongo`, `elasticsearch`, `redis`, `dateutil`, `tldextract`, `matplotlib`
