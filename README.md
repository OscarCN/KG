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
    schemas/        # Entity/pipeline schema definitions (JSON + Python)
    types/          # Type parsers, composite types, registry
    parse_object.py # Core Parser class
  PoC/              # Proof-of-concept implementations
    events.py       # Event extraction from news via GPT-4o (batch + sync)
    event_linking.py# Event deduplication and merging across sources
    newsfeed.py     # News relevance classification and structured extraction
    sentence_pairs_model.py  # Sentence-pair similarity model (PyTorch)
resources/          # Input data files (Excel, prompt contexts)
```

### Schema System (`src/schema/`)

Declarative schema definitions in JSON with a Python normalization pipeline. See [`src/schema/readme_schema.md`](src/schema/readme_schema.md) for full documentation.

- Schemas defined in JSON with string type references, meta descriptions, and support for callable defaults/validators
- Reusable composite types (e.g. `DateRangeFromUnstructured`, `LocationCoords`) auto-resolved across schemas, including inside lists (e.g. `List[DateRangeFromUnstructured]`)
- Parser pipeline: structure mapping → type parsing → defaults → validation

## Infrastructure

| Service | Purpose |
|---------|---------|
| PostgreSQL | Knowledge base storage, geographic queries |
| Redis | LSH cache for fuzzy name matching |
| Elasticsearch | News article indexing and retrieval |
| MongoDB | Crawler/source metadata |
| OpenAI API | Entity extraction (GPT-4o), embeddings |

## Key Dependencies

`openai`, `torch`, `tensorflow_hub`, `sklearn`, `pandas`, `numpy`, `psycopg2`, `pymongo`, `elasticsearch`, `redis`, `dateutil`, `tldextract`, `matplotlib`
