# Entities — Overview

This directory implements the knowledge graph entity pipeline: structured extraction from unstructured text (`extraction/`) and linking the extracted records into canonical entities (`linking/`). Three ontology categories — events, entities/concepts, and themes — share the same schema infrastructure, the same extraction pipeline, and the same target persistence model.

> **Database status: design only.** The unified `kgdb` Postgres database (described in [`media-backend-paid/docs/DATABASE_POSTGRES.md`](../../../../media-backend-paid/docs/DATABASE_POSTGRES.md)) is the **planned** sink for linked records. Nothing is being written there yet — the schema and the persistence model documented in [`linking/readme_linking.md`](linking/readme_linking.md#kg-database-persistence) exist to guide our code/architecture decisions while we iterate on linking approaches.

## Directory Structure

```
entities/
  extraction/                # LLM-based structured extraction from text
    schemas/                 # Entity JSON schemas (one per supertype)
    catalogues/              # Ontology catalogues (event_types.csv, keywords.xlsx)
    prompts/classes/         # Generated extraction prompts (one per supertype, .txt)
    extract.py               # Extraction pipeline
    prompt_generator.py      # Schema → LLM prompt auto-generation
    readme_extraction.md     # Extraction subsystem docs
  linking/                   # Event linking/deduplication via LLM disambiguation
    geocode.py               # Geocoder wrapper (structured Location → level_2_id, coords, geoid)
    link_llm.py              # LLM disambiguator (gemini-2.5-flash-lite) with file cache
    link.py                  # EntityLinker: candidate filter + LLM call (events only)
    run_linking.py           # IPython runner
    readme_linking.md        # Linking subsystem docs (incl. KG database persistence)
  readme_entities.md         # This file (overview)
```

## Subsystems

| Subsystem | Reads | Writes | Docs |
|---|---|---|---|
| **Extraction** (`extraction/`) | News / social-media articles | A flat list of validated entity records, each tagged with `_source_id` and `_supertype` | [`extraction/readme_extraction.md`](extraction/readme_extraction.md) |
| **Linking** (`linking/`) | Extracted records | In-memory / JSON canonical entity records (deduped, geocoded). Persistence to `kgdb` is a designed target, not yet implemented | [`linking/readme_linking.md`](linking/readme_linking.md) |

The full kgdb schema and cross-database conventions are documented in [`media-backend-paid/docs/DATABASE_POSTGRES.md`](../../../../media-backend-paid/docs/DATABASE_POSTGRES.md). The linker's [KG Database Persistence](linking/readme_linking.md#kg-database-persistence) section captures the pieces relevant to the (eventual) write path.

## Ontology Categories

The system distinguishes three broad categories of extractable content. Every supertype schema declares its category via `meta.category`, and that value drives both extraction routing and persistence behaviour:

| Category | Description | Identifying features | Examples |
|---|---|---|---|
| **Event** | A specific, identifiable occurrence at a particular time and place | Location + date/time make each event distinguishable | A concert on a specific date, an accident at a specific location, a protest march |
| **Theme** | A topical classification — any article touching the related subject matches | Optional location (city/state level), no required date — acts as a broad classifier for article content | Security (crime, violence, policing), mobility (traffic, transit), culture (arts, heritage) |
| **Entity / Concept** | A specific, identifiable thing that is not an event | May have a name, location, or other identifying attributes; not necessarily a date | A particular real estate development, a specific technology, a chemical compound, an individual person, a law initiative |

**Currently implemented**: 16 supertypes — 8 **events**, 7 **themes**, and 1 **entity/concept**.

- **Events** (8 supertypes — identifiable single occurrences with location and date): `paid_mass_event`, `robbery_assault_event`, `public_works_event`, `violence_event`, `closures_interruptions_event`, `emergency_event`, `protest_event`, `arrest_event`. Have the `_event` suffix (except `violence_event`, which already had it).
- **Themes** (7 supertypes — topical classifiers without required datetime): `security`, `public_infrastructure`, `civil_protection`, `mobility`, `culture`, `sports`, `civic_participation`. No suffix.
- **Entities / Concepts** (1 supertype — `legislative_initiative`): specific, identifiable things that are not events. Require a `name`, typically include a `jurisdiction` (Location), and date fields describe entity attributes (e.g. `date_introduced`) rather than an occurrence time.

An article may match a theme, an event, and an entity schema simultaneously — all are extracted separately. Extraction details (matching rules, classification, schemas) live in [`extraction/readme_extraction.md`](extraction/readme_extraction.md); linking details (candidate filter, LLM disambiguation, persistence) live in [`linking/readme_linking.md`](linking/readme_linking.md).

**Planned**: more entity/concept supertypes (e.g. real estate developments, persons, technologies). All use the same extraction pipeline (keyword matching → LLM classification → per-class extraction) with schemas that reflect each category's identifying features.

## Future: Class Inheritance

Classes will support inheritance, where a more specific class inherits attributes from a broader one. The current event/theme naming convention is designed to support this:

- **violence_event** inherits from **security** (theme) — a specific shooting inherits the general security topic attributes
- **public_works_event** inherits from **public_infrastructure** (theme)
- **emergency_event** inherits from **civil_protection** (theme)
- **closures_interruptions_event** inherits from **mobility** (theme)
- **paid_mass_event** inherits from **culture** and/or **sports** (themes)
- **protest_event** inherits from **civic_participation** (theme)
- **water_usage_law** (entity) inherits from **legislative_initiative** (entity) — a specific water regulation inherits general initiative attributes

This allows shared attributes and behavior to be defined once at the parent level and specialized at the child level.

In the database (`kgdb.entity_types_kinds_available`), inheritance is currently scoped to **supertype → child type** (e.g. `paid_mass_event` → `concert`). The supertype carries the schema in `metadata_template`; child types inherit and leave it `NULL`. See [Supertypes and types](linking/readme_linking.md#supertypes-and-types-entity_types_kinds_available) in the linking docs for details.
