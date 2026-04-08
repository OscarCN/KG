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
- **`types.py`** — Type parsers (`IntParser`, `DateTimeParser`, `UrlParser`, etc.) with `parse()` and `validate()` methods. `resolve_parser_from_spec()` maps field specs to parsers.
- **`parse_object.py`** — `Parser` class. Core pipeline: `normalize_record()` → `parse_object_structure()` → `traverse_nested(parse_object_types)` → `traverse_nested(_apply_defaults)` → `traverse_nested(_validate)`.
- **`schemas/news.py`** — `NEWS_SCHEMA` with nested `SOURCE_EXTRA_SCHEMA`, `SUPPLIER_SCHEMA`. Supports conditional requirements (e.g., URL required unless type is "impreso") and callable defaults.
- **`schemas/source.py`** — `SOURCE_SCHEMA` for crawler targets with computed defaults (tier from website visits, sitio from domain).

Usage:
```python
from src.schema import Parser, SCHEMA, normalize_record
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

- Schemas are Python dicts (not Pydantic/dataclasses); field specs include `type`, `required`, `default` (can be callable with context), and `enum`.
- External helper module `src.helpers.str_fn` is imported by `types.py` for URL validation and null checking (lives outside this repo).
- External modules `utils.connections`, `utils.es`, `tools.lsh`, `es.es` are imported by PoC scripts (live outside this repo).
- Datetimes default to Mexico City timezone (`America/Mexico_City`).
- Event type taxonomy and prompts are in Spanish.
