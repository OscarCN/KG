# Knowledge Graph — Entity Linking System

Entity linking system that matches entities found in unstructured and semi-structured sources (news articles, social media, websites, contracts, databases) to ground truth entities in a knowledge base. Spanish-language / Mexico-focused.

A document flows **extraction → linking → persistence**: LLM-based structured extraction pulls typed records out of article text, the linker deduplicates and merges them into canonical entities, and the writer persists each linked record into the unified **kgdb** Postgres database. In production this whole chain runs **inline per message** in a long-lived RabbitMQ consumer (`src/listener.py`). See [docs/architecture.md](docs/architecture.md) for the system overview.

## Documentation

| Doc | Covers |
|---|---|
| [docs/architecture.md](docs/architecture.md) | System overview — KB, retrieval, disambiguation, ontology categories, class inheritance, data flow |
| [docs/entities.md](docs/entities.md) | The entity pipeline overview (extraction + linking, ontology categories, the full supertype catalogue) |
| [docs/extraction.md](docs/extraction.md) | LLM-based structured extraction, ontology routing, schemas, prompt generation, the extraction pipeline |
| [docs/linking.md](docs/linking.md) | Event deduplication/merging, geocoding, candidate filter, deterministic gate, LLM disambiguation |
| [docs/storage.md](docs/storage.md) | The kgdb persistence model and the streaming pipeline (single source of truth for storage/kgdb) |
| [src/schema/readme_schema.md](src/schema/readme_schema.md) | The schema system (JSON definitions + the Python normalization pipeline) |
| Tags (decoupled) | Customer-anchored stances + per-event claims — docs (`readme_tags.md`, `tags_overview.md`, `tags_impl_plan.md`) live with the tags code, which is moving to its own repository |

Full kgdb schema and cross-database conventions live in [`media-backend-paid/docs/DATABASE_POSTGRES.md`](../../media-backend-paid/docs/DATABASE_POSTGRES.md). The local **dev kgdb** (Docker Postgres on `:5334`) setup is in [`media/dev/docs/db/runbook.md`](../../dev/docs/db/runbook.md).

## Repository Structure

```
src/
  listener.py       # Streaming RabbitMQ consumer: documents → extract → link → persist (kgdb)
  processed_store.py # Redis doc-level idempotency guard (skip already-processed doc ids; 2-week TTL)
  schema/           # Schema system for data normalization (docs: src/schema/readme_schema.md)
    schemas/        # Pipeline schema definitions (JSON + Python)
    types/          # Type parsers, composite types, registry
    parse_object.py # Core Parser class
  entities/         # Entity extraction and linking (overview: docs/entities.md)
    run_entities.py # Integration runner: streams documents through extraction, then linking
    document.py     # record_to_article: map a raw document envelope to the extractor's input (shared)
    extraction/     # LLM-based structured extraction from text (docs: docs/extraction.md)
      schemas/      # Entity schemas (one per supertype, JSON)
      catalogues/   # Ontology catalogues (event types CSV, keywords Excel)
      prompts/classes/ # Generated LLM extraction prompts (one per supertype)
      extract.py    # Extraction pipeline
      prompt_generator.py # Schema → LLM prompt auto-generation
    linking/        # Event linking/deduplication and KG database persistence (docs: docs/linking.md, docs/storage.md)
      geocode.py    # Thin client for deepriver's geocoder microservice (structured-input)
      link_llm.py   # LLM disambiguator (gemini-2.5-flash-lite) with file cache
      index.py      # CandidateIndex + RecordStore protocols + in-memory implementations
      kgdb_retrieval.py # kgdb-backed CandidateIndex (SQL column-reconstruction) + RecordStore (reads entities.metadata)
      mx_states.py  # Static Mexican-state catalogue (geo partition-key normalization + fallback)
      strategy.py   # GeoEventStrategy: per-supertype identification lifecycle (enrich → keys → adjudicate → merge)
      link.py       # EntityLinker: envelope parse + strategy orchestration (events only). Exposes link_one(raw) → LinkResult for streaming callers.
      persistence.py# KgdbWriter: idempotent write of a linked record into kgdb (Step Zero batch/stream writer)
      run_linking.py# IPython runner: tests linking from extracted_raw/*.json fixtures → linked/*.json
    tags/           # Customer-anchored stances + per-event claim clusters (Stage 1, in-memory)
      models/       # Pure data structures: customer.py, source_item.py, stance_catalog.py, claim_catalog.py
      bootstrap.py / tagging.py / stance_adjudicator.py / claim_clusterer.py / apply.py
      retrieval.py / persistence.py / stats.py
      prompts/      # Spanish prompts for the four LLM phases
      tags_overview.md / tags_impl_plan.md / readme_tags.md
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
  seed_ontology_rules.py     # Seeds kgdb ontology_matching_rules from keywords.xlsx (full refresh; prod rule source)
  persist_linked.py          # Step Zero: writes a data/linked/<stem>.json fixture into kgdb via KgdbWriter
  enqueue_from_es.py         # Testing producer: ES date-window fetch (geo/precision/category filter) → kg RabbitMQ doc queue
  publish_document.py        # Publish a JSON document file to a RabbitMQ queue (dev-vhost listener loop)
cache/              # Extraction + linking LLM-call cache (sha256-keyed, auto-generated)
docs/
  architecture.md   # System overview
  entities.md / extraction.md / linking.md / storage.md  # Subsystem docs
  todos/            # Roadmap / design TODOs — one self-contained file per TODO
```

## Subsystems at a glance

- **Schema system** (`src/schema/`) — declarative JSON schema definitions with a Python normalization pipeline (structure mapping → type parsing → defaults → validation) and auto-resolved composite types. See [src/schema/readme_schema.md](src/schema/readme_schema.md).
- **Entity extraction** (`src/entities/extraction/`) — three-step flow (keyword matching → LLM classification → per-class extraction) over a three-level ontology (rule → class → supertype → schema), 16 supertypes (9 events + 6 themes + 1 entity/concept), schema-driven prompt generation. See [docs/extraction.md](docs/extraction.md).
- **Entity linking** (`src/entities/linking/`) — deduplicates and merges extracted **events** into canonical records (geocode → candidate filter → hard geo gate → deterministic gate / LLM disambiguation → merge or create), caching geocode and LLM responses on disk. See [docs/linking.md](docs/linking.md).
- **Persistence & streaming** (`src/listener.py`, `linking/persistence.py`, `linking/kgdb_retrieval.py`) — the production streaming consumer plus the `KgdbWriter` write model into kgdb, kgdb-backed cross-worker dedup, the merge mechanism, three idempotency layers, and per-document extractions. See [docs/storage.md](docs/storage.md).
- **Tags** (`src/entities/tags/`) — decoupled customer-anchored stances + per-event claim clusters, moving to its own repository (the tags code and its docs live there).

## Infrastructure

| Service | Purpose |
|---------|---------|
| PostgreSQL | Unified **kgdb** knowledge graph (entities/events) + geographic queries; candidate retrieval. Local dev DB on Docker `:5334` (see [`media/dev/docs/db/runbook.md`](../../dev/docs/db/runbook.md)) |
| RabbitMQ | Document queue feeding the streaming listener (`src/listener.py`) |
| Redis | Document-level dedup guard (`processed_store.py`) — atomic in-flight claim (`SET NX EX`, short TTL) plus a 2-week processed marker, so concurrent duplicate deliveries can't both be processed. *(Legacy: LSH name-match cache — not wired into the current pipeline.)* |
| Elasticsearch | News article indexing and retrieval (source of the document corpus) |
| MongoDB | Crawler/source metadata |
| OpenRouter | LLM calls for extraction (`OPENROUTER_MODEL`), prompt generation (`OPENROUTER_GENERATION_MODEL`), prompt feedback (`OPENROUTER_FEEDBACK_MODEL`), event linking (`OPENROUTER_LINKER_MODEL`), tags bootstrap / tagger / adjudicator / clusterer (`OPENROUTER_BOOTSTRAP_MODEL`, `OPENROUTER_TAGGER_MODEL`, `OPENROUTER_ADJUDICATOR_MODEL`, `OPENROUTER_CLUSTERER_MODEL`) |
| OpenAI API | Embeddings |

## Key Dependencies

`openai`, `requests`, `pika`, `psycopg2`, `redis`, `pandas`, `numpy`, `nltk`, `openpyxl`, `dateutil`, `tldextract`, `elasticsearch` — plus PoC/ML extras (`torch`, `tensorflow_hub`, `sklearn`, `matplotlib`, `pymongo`). Pinned in [`requirements.txt`](requirements.txt) (core vs. optional).

## Roadmap / TODOs

Design and roadmap TODOs live in [`docs/todos/`](docs/todos/) — **one self-contained file per TODO**.

- [**Productionization: streaming KG → live kgdb**](docs/todos/productionization_streaming_kg.md) — go-live checklist for running the listener continuously against **live kgdb**. Decisions locked: **production-only** writer (kgdb is shared append-only ground truth — no staging/prod double-write; dev kgdb is the test target), **single worker first** (until [canonical reconciliation](docs/todos/canonical_reconciliation.md) lands), post-`gp3` **firehose MVP** producer. Phased work: ontology keywords → kgdb table, apply live schema/index migrations, prod config/secrets, the `gp3`→kg-queue producer seam, Dockerfile/k8s deploy, observability, canary cutover.
- [Persist per-document extractions (pre-merge ground truth)](docs/todos/persist_document_extractions.md) — we only store the **merged canonical** today; what each individual document said before the merge (and anything the linker drops/skips — themes, entities, no-date records) is **not persisted** (only ephemeral `cache/extraction/` local files). Add a dedicated `document_extractions` kgdb table (one row per extracted record, with a nullable `linked_entity_id`) so we keep provenance, can **re-link without re-paying the LLM**, and have training/debug data. Design locked (dedicated table); part of productionization Phase 1.
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
