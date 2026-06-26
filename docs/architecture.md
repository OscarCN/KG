# Architecture — system overview

Knowledge graph entity-linking system: it matches entities found in unstructured and
semi-structured sources (news articles, social media, websites, contracts, databases) to
ground-truth entities in a knowledge base.

## Data flow

A document flows **extraction → linking → persistence**, streamed one message at a time:
LLM-based structured extraction pulls typed records out of the article text
([entities.md](entities.md), [extraction.md](extraction.md)); the linker deduplicates and
merges those records into canonical entities ([linking.md](linking.md)); and the writer
persists each linked record into the unified **kgdb** Postgres database
([storage.md](storage.md)). In production this whole chain runs **inline per message** in a
long-lived RabbitMQ consumer (`src/listener.py`) — see [storage.md](storage.md) for the
streaming pipeline.

## System Components

### Knowledge Base (KB)

Two knowledge bases:

- **Geographic KB** — Hierarchical geographic entities (countries, provinces, cities,
  neighborhoods, streets, places) linked by "is in" relations. Each entity has coordinates,
  shape, and aliases.
- **Entities/Events KB** — Entities, concepts, themes, and events typed by an ontology. See
  [Ontology categories](#ontology-categories) below for the distinction between these types.
  Each entry has attributes defined by its ontology class schema.

Each entity has an **ontology class** that defines its schema (attributes, identifying
features) and how it should be uniquely described. Ontology schemas are defined in JSON and
parsed by the schema system ([`../src/schema/readme_schema.md`](../src/schema/readme_schema.md)).

### Retrieval

Multiple retrieval strategies depending on entity type:

- **Name similarity** — Locality-sensitive hashing (LSH) via Redis for efficient fuzzy name
  matching
- **Geographic** — Coordinate-based queries (point-in-shape, nearest) via PostgreSQL
- **Semantic** — Embedding-based similarity on descriptions via vector database

### Disambiguation / Linking

Deciding the correct entity from a set of candidates using features derived from:

- Language (descriptions, narratives)
- Location (coordinates, addresses)
- Time, taxonomies, identifiers

## Ontology Categories

The system classifies content into three broad ontology categories, each with different
identifying characteristics. Every supertype schema declares its category via `meta.category`,
and that value drives both extraction routing and persistence behaviour.

| Category | Description | Identifying features | Examples |
|----------|-------------|---------------------|----------|
| **Event** | A specific, identifiable occurrence that happened at a particular time and place | Location + date/time make each event distinguishable from others | A concert, an accident, a protest, an arrest |
| **Theme** | A topical classification — any article that touches or discusses a related subject matches | Optional location (city/state level), no required date — acts as a broad classifier for article content | Security (crime, violence, policing), mobility (traffic, transit), culture (arts, heritage) |
| **Entity/Concept** | A specific, identifiable thing that is not an event | May have a name, location, or other identifying attributes, but not necessarily a date | A real estate development, a specific technology, a chemical compound, an individual person, a law initiative |

The system implements **events** (9 supertypes — identifiable single occurrences with a
location and date), **themes** (6 supertypes — topical classifiers without required datetime),
and **entities/concepts** (1 supertype — `legislative_initiative`, with more planned). A theme
matches whenever an article addresses, reports on, or touches any subject within its domain —
whether through a specific event, a complaint, statistics, policy discussion, or a passing
mention. An entity/concept matches only when the article refers to a specific, identifiable item
of that type (with a proper name or distinguishing attributes), not a generic mention of the
domain. An article may match a theme, an event, and an entity schema simultaneously — all are
extracted separately. Events have `_event` suffix in their supertype name (e.g. `arrest_event`,
`emergency_event`); themes and entities do not (e.g. `security`, `mobility`,
`legislative_initiative`).

The extraction pipeline (keyword matching → LLM classification → per-class extraction) is
designed to work with all three categories. The classification prompt presents candidates in up
to three groups (Eventos, Temas, Entidades/Conceptos) with their own selection criteria, and
per-class extraction runs the schema bound to the confirmed class's supertype. The full
supertype catalogue lives in [entities.md](entities.md) and [extraction.md](extraction.md).

## Future: Class Inheritance

Classes will support inheritance, where a more specific class inherits attributes from a broader
one. The current event/theme naming convention supports this:

- **violence_event** inherits from **security** (theme) — a specific shooting inherits the
  general security topic attributes
- **public_works_event** inherits from **public_infrastructure_event** (event)
- **emergency_event** inherits from **civil_protection** (theme)
- **closures_interruptions_event** inherits from **mobility** (theme)
- **protest_event** inherits from **civic_participation** (theme)
- **water_usage_law** (entity) inherits from **legislative_initiative** (entity) — a specific
  water regulation inherits general legislative initiative attributes

This allows shared attributes and behavior to be defined once at the parent level and
specialized at the child level. In kgdb (`entity_types_kinds_available`), inheritance is
currently scoped to **supertype → child type** — see
[Supertypes and types](storage.md#supertypes-and-types-entity_types_kinds_available) in the
storage docs.

A related future direction is **multi-class entities** — a single entity instantiating more than
one ontology class simultaneously (e.g. an `arrest_event` that is also a `violence_event`). The
`entity_types` table on the kgdb side is already a many-to-many and supports it on the schema
side. The open questions are at the validation layer (which class's schema does
`entities.metadata` conform to when an entity carries several?) and at the linker (does
multi-class membership widen the candidate filter?). We'll address it alongside inheritance —
until then, the working assumption is one supertype per entity.

## Where to go next

- [entities.md](entities.md) — the entity pipeline overview (extraction + linking, ontology
  categories, the full supertype catalogue)
- [extraction.md](extraction.md) — LLM-based structured extraction, ontology routing, prompt
  generation
- [linking.md](linking.md) — event deduplication/merging, geocoding, candidate filter, LLM
  disambiguation
- [storage.md](storage.md) — the kgdb persistence model and the streaming pipeline
- [`../src/schema/readme_schema.md`](../src/schema/readme_schema.md) — the schema system
