# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

Entity linking system for a knowledge graph (KG). Links entities (events, people, organizations, locations, etc.) from unstructured/semi-structured sources (news, social media) to ground truth entities in a knowledge base. Spanish-language / Mexico-focused.

Three main subsystems:
1. **Schema layer** (`src/schema/`) — Declarative schema definitions and a `Parser` that normalizes raw records through structure mapping → type parsing → defaults → validation.
2. **Entity extraction PoCs** (`src/PoC/`) — GPT-4o-based extraction of structured events from news, relevance classification, and sentence-pair similarity training.
3. **Entity linking PoC** (`src/PoC/event_linking.py`) — Deduplication and merging of events across sources using LSH, geographic distance, date overlap, and type matching.

## Architecture

### Schema system (`src/schema/`)
- **`schemas/*.json`** — Schema definitions in JSON with string type references, meta descriptions, `default_fn`/`required_fn` for callable defaults/validators.
- **`schemas/read_schema.py`** — Loads JSON schemas, resolves type strings to Python types, wires callables, auto-resolves composite type dependencies.
- **`schemas/source.py`, `schemas/news.py`** — Define callable defaults and load their respective JSON files.
- **`types/`** — Type parsers (`IntParser`, `DateTimeParser`, `UrlParser`, etc.) with `parse()` and `validate()` methods. `registry.py` maps type strings to Python types and Python types to parsers.
- **`types/composite_types.json`** — Reusable multi-field types (`LocationCoords`, `DateRangeFromUnstructured`, etc.) auto-resolved when referenced.
- **`parse_object.py`** — `Parser` class. Core pipeline: `normalize_record()` → `parse_object_structure()` → `traverse_nested(parse_object_types)` → `traverse_nested(_apply_defaults)` → `traverse_nested(_validate)`.

Usage:
```python
from src.schema import SCHEMAS, META, normalize_record
normalized = normalize_record(record, "Source")
```

### PoC layer (`src/PoC/`)
- **`events.py`** — Extracts structured events from ES-indexed news using OpenAI Batch API or sync calls. Defines a detailed Spanish-language prompt with 27 event types and structured location/date/price fields.
- **`event_linking.py`** — Compares and merges events using: LSH Jaccard distance on names, Euclidean distance on coordinates, date range overlap, and type compatibility. Creates event IDs as `{date}{geoid}_{random}`.
- **`newsfeed.py`** — Relevance classification (1-5 scale), structured extraction, and sentiment analysis pipeline. Connects to ES, PostgreSQL, MongoDB.
- **`sentence_pairs_model.py`** — Trains a PyTorch `TopicClassifier` (residual highway network) on sentence-pair embeddings (Universal Sentence Encoder + OpenAI embeddings) to detect co-referring sentences.

## Key Dependencies

- **AI/ML**: `openai` (GPT-4o, embeddings), `torch`, `tensorflow_hub`, `sklearn`
- **Data**: `pandas`, `numpy`, `dateutil`
- **Databases**: `psycopg2` (PostgreSQL), `pymongo` (MongoDB), `elasticsearch`, `redis` (LSH cache)
- **Other**: `tldextract`, `zoneinfo`, `matplotlib`

No `requirements.txt` or `pyproject.toml` exists yet; dependencies must be installed manually.

## Running

All PoC scripts require environment variables for OpenAI API credentials and running database services (Elasticsearch, PostgreSQL, MongoDB, Redis). Scripts are run directly:

```bash
python src/PoC/events.py
python src/PoC/event_linking.py
python src/PoC/newsfeed.py
python src/PoC/sentence_pairs_model.py
```

The schema system can be used as a library with no external services.

## Important Conventions

- Schemas are defined in JSON files (`schemas/*.json`), loaded via `load_schema()`. Callable defaults/validators stay in Python companion files and are referenced by name (`default_fn`, `required_fn`).
- Reusable composite types live in `types/composite_types.json` and are auto-resolved by the loader.
- External modules `utils.connections`, `utils.es`, `tools.lsh`, `es.es` are imported by PoC scripts (live outside this repo).
- Datetimes default to Mexico City timezone (`America/Mexico_City`).
- Event type taxonomy and prompts are in Spanish.
- After each non-trivial change/implementation/fix, add or change the relevant documentation in README.md and/or any pertinent documentation sub-file (e.g. schema/readme_schema.md). After every change check that documentation files are kept consistent, and that none of their content is kept outdated
- Keep all documentation files well organized, consistent, clear, complete and lean.
