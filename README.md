# Knowledge Graph — Entity Linking System

Entity linking system that matches entities found in unstructured and semi-structured sources (news articles, social media, websites, contracts, databases) to ground truth entities in a knowledge base.

## System Components

### Knowledge Base (KB)

Two knowledge bases:

- **Geographic KB** — Hierarchical geographic entities (countries, provinces, cities, neighborhoods, streets, places) linked by "is in" relations. Each entity has coordinates, shape, and aliases.
- **Entities/Events KB** — General entities (people, organizations, companies, products, technologies, regulations) and events (concerts, protests, accidents, congresses) typed by an ontology. Events always have a date/date range and may have location, description, and other attributes.

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
```

### Schema System (`src/schema/`)

Declarative schema definitions in JSON with a Python normalization pipeline. See [`src/schema/readme_schema.md`](src/schema/readme_schema.md) for full documentation.

- Schemas defined in JSON with string type references, meta descriptions, and support for callable defaults/validators
- Reusable composite types (e.g. `DateRangeFromUnstructured`, `DateFromUnstructured`, `Location`, `PriceRange`, `CasualtyCount`) auto-resolved across schemas, including inside lists (e.g. `List[DateRangeFromUnstructured]`)
- Parser pipeline: structure mapping → type parsing → defaults → validation

### Entity Extraction (`src/entities/`)

LLM-based structured extraction from unstructured text (news articles, social media). See [`src/entities/readme_entities.md`](src/entities/readme_entities.md) for full documentation.

- **Ontology**: three-level hierarchy (matching rule → event type → supertype → schema) with rules in Excel (`keywords.xlsx`) and type mapping in CSV (`event_types.csv`)
- **8 supertypes**: paid_mass_event, robbery_assault (incl. kidnapping), public_works (incl. trash, water, sinkhole, public road), violence_event, closures_interruptions, emergency (incl. pedestrian hit), protest, arrest
- **Three-step extraction flow**: keyword matching → LLM classification (filters candidate classes to those actually discussed) → per-class LLM extraction (one call per confirmed class, scoped to that event type)
- **Schema-driven prompt generation**: extraction prompts auto-generated from JSON schemas via LLM using a generate+feedback loop (`prompt_generator.py`). Each prompt is built from three context layers — class `meta.description`, field `description`, and composite type descriptions (e.g. `DateRangeFromUnstructured` contributes approximate-date and `precision_days` instructions). `robbery_assault.txt` serves as the style exemplar. Write schema descriptions carefully — they directly affect prompt quality.
- **Same schema infrastructure**: entity schemas use the same JSON format, `load_schema()`, `Parser`, and composite types as pipeline schemas

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
