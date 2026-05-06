# Entities — Extraction

LLM-based structured extraction from unstructured text (news articles, social media). Inputs: article text + metadata. Outputs: a flat list of validated entity records, each tagged with `_source_id` and `_supertype`, ready for the linker.

For an overview of the broader pipeline, ontology categories, and how extraction relates to linking, see [`../readme_entities.md`](../readme_entities.md).

## Directory Structure

```
extraction/
  schemas/                   # Entity JSON schemas (one per supertype)
    # Events
    paid_mass_event.json
    robbery_assault_event.json
    public_works_event.json
    violence_event.json
    closures_interruptions_event.json
    emergency_event.json
    protest_event.json
    arrest_event.json
    # Themes
    security.json
    public_infrastructure.json
    civil_protection.json
    mobility.json
    culture.json
    sports.json
    civic_participation.json
    # Entities / concepts
    legislative_initiative.json
  catalogues/                # Ontology catalogues
    event_types.csv          # event_type → supertype mapping with labels
    keywords.xlsx            # Matching rules: class, keywords, filters (Excel)
    keywords.csv             # (legacy reference — not used by the system)
  prompts/
    classes/                 # Generated extraction prompts (one per supertype, .txt)
  extract.py                 # Extraction pipeline: matching, LLM calls, parsing
  prompt_generator.py        # Schema → LLM prompt auto-generation
  readme_extraction.md       # This file
```

## Ontology Routing

Three-level hierarchy for routing articles to the correct extraction schema:

```
keyword → class → supertype → schema
```

### Class types (`catalogues/event_types.csv`)

Each class maps to exactly one **supertype** (superclass). The supertype determines which JSON schema is used for extraction.

**Event supertypes** — identifiable single occurrences with location and datetime:

| Supertype | Event types | Schema |
|---|---|---|
| `paid_mass_event` | concert, festival, party, fair, inauguration, sports_event, religious_event, cultural_event, congress, exposition, conference, convention | `paid_mass_event.json` |
| `robbery_assault_event` | robbery, assault, kidnapping, security_event | `robbery_assault_event.json` |
| `public_works_event` | pothole, street_lighting, paving, public_transport, infrastructure, trash_complaint, sinkhole, public_road | `public_works_event.json` |
| `violence_event` | shooting, attack, homicide, confrontation | `violence_event.json` |
| `closures_interruptions_event` | blockade, closure, suspension_of_operations | `closures_interruptions_event.json` |
| `emergency_event` | fire, crash, explosion, flood, accident, pedestrian_hit, emergency_general | `emergency_event.json` |
| `protest_event` | protest | `protest_event.json` |
| `arrest_event` | arrest, detention | `arrest_event.json` |

**Theme supertypes** — general discourse topics, no required datetime:

| Supertype | Theme types | Schema |
|---|---|---|
| `security` | crime_trends, law_enforcement, public_safety, security_policy | `security.json` |
| `public_infrastructure` | infrastructure_conditions, urban_services, water_management, water_issue, waste_management, transportation_infrastructure, urban_planning | `public_infrastructure.json` |
| `civil_protection` | emergency_preparedness, disaster_trends, accident_statistics, risk_assessment, civil_protection_policy | `civil_protection.json` |
| `mobility` | transit_disruptions, road_conditions, transportation_planning, traffic_patterns, public_transit | `mobility.json` |
| `culture` | cultural_life, arts_scene, festival_landscape, heritage, cultural_policy | `culture.json` |
| `sports` | sports_landscape, competition_coverage, sports_infrastructure, athlete_profile, league_overview | `sports.json` |
| `civic_participation` | social_movements, activism, citizen_engagement, political_participation, community_organizing | `civic_participation.json` |

**Entity/Concept supertypes** — specific, identifiable things that are not events or themes:

| Supertype | Entity types | Schema |
|---|---|---|
| `legislative_initiative` | law_initiative, reform_initiative, decree, regulation, legislative_agreement, ratification | `legislative_initiative.json` |

### Matching rules (`catalogues/keywords.xlsx`)

Matching rules are defined in an Excel file with the same layout as `resources/kg/events_qro.xlsx`. Each row is an independent matching rule. Within a row, all non-empty columns are **AND'd** together. Across rows, matches are **OR'd** — a document matches if any row matches.

| Column | Purpose | Operator |
|---|---|---|
| `section`, `subsection`, `tag` | Labeling only — not used in matching | — |
| `class` | Ontology class assigned on match (e.g. `street_lighting`, `concert`) | — |
| `kw` | Quoted comma-separated keywords (e.g. `"robo","robar"`) — matched with **stemming** (word-level) | OR (any stemmed kw in text) |
| `phrase` | Quoted comma-separated phrases (e.g. `"evento deportivo"`) — matched **exactly** (no stemming, substring) | OR (any phrase in text) |
| `not` | Quoted comma-separated exclusion keywords — matched exactly (no stemming) | NOT (text must not contain any) |
| `location` | Location keywords | Not used currently |
| `categories` | Pipe-separated categories (e.g. `Deportes\|Cultura`) | OR (doc must have any) |
| `document_type` | Comma-separated doc types (e.g. `news,facebook`) | OR (doc type must match any) |
| `dismiss_categories` | Pipe-separated categories to exclude | NOT (doc must not have any) |
| `period` | History search period (`d`, `w`, `m`, `y`) | Not used currently |
| `bbox` | Bounding box for geocoded content | Not used currently |

**Matching logic per row**: `(has any kw OR has any phrase) AND (has no 'not' kw) AND (has any category) AND (has no dismiss_category) AND (doc type matches)`. Empty columns are skipped (always pass). `kw` and `phrase` within the same row are OR'd — matching either satisfies the text condition. `kw` uses the NLTK Spanish Snowball stemmer for word-level matching (e.g. "robaron" matches kw "robo"); `phrase` uses exact normalized substring matching (e.g. "cierre de calle" matches only that exact phrase).

Multiple keywords can map to the same ontology class across different rows with different filter combinations. An article matching rules from different supertypes will be sent to multiple extraction schemas.

The legacy `keywords.csv` is kept for reference but is not used by the system.

## Schemas

Entity schemas live in `schemas/` and use the same JSON format as the pipeline schemas in `src/schema/schemas/`. They are loaded with the same `load_schema()` infrastructure.

Each schema defines:
- **`meta.category`**: `"event"`, `"theme"`, or `"entity"`. Drives routing in `EntityExtractor.classify()`.
- **`meta.description`**: describes what this entity type represents. Used in two LLM-facing stages: (1) the classification step, where it helps the LLM decide whether an article actually refers to this event type, and (2) prompt generation, where it becomes the LLM system message. Write these descriptions carefully — they directly affect classification accuracy and extraction quality.
- **`meta.example`**: a complete example of the expected JSON output (included in the generated prompt). Composite type fields must include ALL subfields defined by the type, with null for absent values — omitting fields causes generated prompts to miss them during extraction.
- **`schema`**: field definitions, each with:
  - `type`: data type (same type system as `src/schema/types/`)
  - `description`: extraction instruction for the LLM — doubles as field documentation
  - `enum`: allowed values (for EnumStr fields, rendered as a catalogue in the prompt)
  - `required`: whether the field must be present

### Shared composite types

Schemas reference composite types from `src/schema/types/composite_types.json`:

| Type | Fields | Used for |
|---|---|---|
| `Location` | country, state, city, neighborhood, zone, street, number, place_name | Event/incident location |
| `DateRangeFromUnstructured` | date_range (PeriodDates), timezone, mention, precision_days | When events occurred (date ranges) |
| `DateFromUnstructured` | date, mention, precision_days | Single dates (e.g. completion dates) |
| `PriceRange` | mention, lower, upper, currency | Ticket prices, costs |
| `Attendance` | mention, estimate | Event attendance |
| `VenueCapacity` | mention, capacity | Venue capacity |
| `CasualtyCount` | mention, dead, injured, missing | Incident casualties |
| `CountMention` | mention, count, confidence_range | Generic numeric count (victims, detainees, vehicles) |
| `PersonReference` | name, role, organization | People mentioned |

### Common fields (event supertypes)

All 8 event schemas share these fields (with the same semantics):

- `event_type` (EnumStr) — from the supertype's catalogue
- `event_subtype` (str) — free-form specific subtype
- `status` (EnumStr) — current status
- `name` (str) — event/incident name
- `description` (str) — brief description
- `tags` (List[str]) — keywords
- `context` (str) — surrounding context
- `relevance` (EnumStr) — 1/2/3 relevance in the article
- `date_range` (DateRangeFromUnstructured) — when it happened
- `location` (Location) — where it happened

### Common fields (theme supertypes)

All 7 theme schemas share these fields:

- `theme_type` (EnumStr) — from the supertype's catalogue
- `theme_subtype` (str) — free-form specific subtype
- `description` (str, required) — summary of how the article touches or discusses the theme
- `tags` (List[str]) — keywords and topics
- `context` (str) — broader context, trends, policy
- `relevance` (EnumStr) — 1/2/3 relevance in the article
- `sentiment` (EnumStr) — tone: positive/negative/neutral/mixed/alarming/hopeful
- `location` (Location, optional) — geographic area the theme pertains to
- `related_subtopics` (List[str]) — specific issues discussed under this theme
- `time_scope` (DateRangeFromUnstructured) — temporal scope of the discourse as a structured date range with original mention and precision

### Common fields (entity/concept supertypes)

Entity schemas share this convention (parallel to the event/theme commons). Not all fields are required for every entity supertype, but the set establishes the baseline shape for the category:

- `entity_type` (EnumStr, required) — from the supertype's catalogue
- `entity_subtype` (str) — free-form specific subtype
- `name` (str, required) — the identifying name or title of the entity
- `aliases` (List[str]) — alternative names or short forms
- `description` (str, required) — brief description of what the entity is or proposes
- `tags` (List[str]) — keywords
- `context` (str) — broader context, debate, related developments
- `relevance` (EnumStr) — 1/2/3 relevance in the article
- `status` (EnumStr) — current status (supertype-specific enum)
- `jurisdiction` (Location, optional) — geographic scope the entity applies to
- `date_introduced` (DateFromUnstructured, optional) — when the entity was created/filed/founded, if relevant
- `identifiers` (List[str]) — official identifiers (expediente, folio, URL, etc.)
- `related_subjects` (List[str]) — other domains touched by the entity

Entity supertypes typically add domain-specific fields (e.g. `legislative_body`, `authors`, `affected_laws` for `legislative_initiative`). Date fields describe attributes of the entity, not an event datetime — the extraction prompt frames them as "date of introduction" / "date of creation" rather than "when it happened".

## Prompt Generation (`prompt_generator.py`)

Auto-generates Spanish-language extraction prompts from JSON schemas using a two-step LLM process (generate + feedback/revision).

### Context assembly (`PromptGenerationContextManager`)

For a given supertype, gathers three layers of context into a structured dict that the generation LLM uses to craft the prompt:

1. **Class-level**: `meta.description` (what this entity type represents — drives both the LLM classification step and the generated prompt's system message) and `meta.example` (complete JSON output example included in the prompt)
2. **Field-level**: each field's `description`, `type`, `required`, `enum` values — these become per-field extraction instructions in the generated prompt
3. **Composite type-level**: for fields referencing composite types (e.g. `DateRangeFromUnstructured`, `Location`, `PriceRange`), the type's `meta.description` and per-field descriptions are included — these contribute structural instructions (e.g. approximate date handling with `precision_days`, the `mention` pattern for quoting original text, `Location.place_name` guidance)

### Generation template

A prompt sent to a generation LLM that:
- Receives the schema context + `paid_mass_event.txt` as reference style exemplar
- Translates all English descriptions to instructional Spanish
- Renders `EnumStr` fields as catalogues with `"value" — Spanish label: description`
- Expands composite types with complete JSON structure examples (ALL subfields must appear, with null for absent values) and `mention` pattern ("texto tal cual se menciona en la nota")
- Injects global rules: null for missing fields, don't invent events, JSON list response format
- Injects type-specific rules: `DateRangeFromUnstructured`/`DateFromUnstructured` approximate date instructions and `precision_days` examples, `Location` place_name guidance
- Includes template variables `{date_now}`, `{source_type}`, `{body}` for runtime substitution

### Feedback loop

The generated draft is sent to a separate feedback LLM (potentially a different, more powerful model) that checks completeness, schema consistency, Spanish quality, format, template variables, and — critically — composite type field completeness (every subfield of every composite type must appear in JSON examples, with null for absent values). Feedback is then applied by the generation LLM to produce the final prompt.

### Output

Saved to `prompts/classes/{supertype}.txt` in `SYSTEM:/USER:/USER:` format, ready for `_load_prompt()` to load at extraction time.

### Runtime context variables

Generated prompts include these template variables, substituted by `extract.py` at runtime:

| Variable | Source | Description |
|---|---|---|
| `{date_now}` | `datetime.now()` | Current date (dd/mm/YYYY), used as temporal context for date interpretation |
| `{source_type}` | `article["source_type"]` | Source type (e.g. "news", "facebook"). For social media, dates may not be explicitly mentioned in the text — the LLM may infer them from the publication date or other context |
| `{body}` | `article["text"]` | The article text to extract from |

### Usage

```python
from src.entities.extraction.prompt_generator import PromptGeneration

gen = PromptGeneration()
gen.generate("emergency_event")  # generate one supertype
gen.generate_all()            # generate all supertypes
```

### LLM configuration

| Variable | Required | Default | Description |
|---|---|---|---|
| `OPENROUTER_GENERATION_MODEL` | no | `anthropic/claude-opus-4.6` | Model for prompt generation |
| `OPENROUTER_FEEDBACK_MODEL` | no | `openai/gpt-5.4` | Model for prompt feedback/review |

## Extraction Pipeline (`extract.py`)

```
Article → rule matching → LLM classification → per-class LLM extraction → JSON parse → schema validation → structured output
```

### Three-step flow

1. **Keyword matching** — `Ontology.match()` evaluates all keyword/phrase/category rules against the article, returning a set of candidate ontology classes.
2. **LLM classification** — `EntityExtractor.classify()` presents the LLM with the article and the candidate classes split by category (read from each schema's `meta.category`) into up to three groups with different selection criteria. **Events** are selected only if the article reports a specific identifiable occurrence. **Themes** are selected whenever the article touches, mentions, or discusses any related subject — even as context or in passing. **Entities/Concepts** are selected only when the article refers to a specific identifiable item of that type (with a proper name or distinguishing attributes), not by a generic mention of the domain. A single article can match multiple classes across groups (e.g. a reform article may confirm both the `reform_initiative` entity class, the `security_policy` theme, and the `protest` event class if a protest is reported).
3. **Per-supertype extraction** — Confirmed classes are grouped by supertype. When multiple classes share a supertype (e.g. `pedestrian_hit` + `emergency_general` → `emergency_event`), extraction runs once without a class focus so the LLM extracts all relevant entries under that schema. When a supertype has a single confirmed class, extraction runs with a focus instruction scoping to that class. Results are parsed, validated, and cached per `(article URL, class or supertype)` pair (see [Cache](#cache)).

This flow avoids redundant extraction calls for keyword matches that don't correspond to actual content, and produces cleaner results when an article triggers keywords from multiple unrelated classes.

### Components

- **`Ontology`** — loads `event_types.csv` and `keywords.xlsx`, evaluates matching rules (keywords, exclusions, categories, document type) against articles, resolves matched ontology classes to supertypes, and provides class descriptions for the classification step. Class descriptions (from `meta.description`) tell the classifier what ontology category each class belongs to and what distinguishes extractable items.
- **`EntityExtractor`** — orchestrates the pipeline: `match()` finds candidate classes, `classify()` asks the LLM which classes actually apply, `extract_supertype()` extracts events scoped to a specific class, `extract()` runs the full three-step flow.
- **`call_llm()`** — sends messages to an LLM via OpenRouter (`src/llm/openrouter/`). Requests JSON mode for reliable parsing. Model and API key configured via environment variables (see below).
- **`_load_prompt()`** — reads prompt files from `prompts/classes/`, substitutes context variables (`{date_now}`, `{source_type}`, `{body}`).
- **`_validate_entity()`** — runs each extracted entity through the schema `Parser` for type coercion and validation.
- **`_cache_read()` / `_cache_write()`** — file-based extraction cache keyed by `sha256(article_url|class_name)`, stored as JSON in `cache/`.
- **`_classify_cache_read()` / `_classify_cache_write()`** — file-based classification cache keyed by `sha256(classify|article_url|sorted_classes)`, stored as JSON in `cache/`.

### Usage

```python
from src.entities.extraction.extract import EntityExtractor

extractor = EntityExtractor()
results = extractor.extract({
    "text": "Dos sujetos armados asaltaron una tienda en el centro...",
    "title": "Asalto en León",
    "categories": ["Seguridad"],
})
# results: list of validated entity dicts, each tagged with _source_id and _supertype
```

For each article, keyword matching may produce many candidate classes, but only those confirmed by the LLM classification step proceed to extraction. Each confirmed class gets its own extraction call scoped to that event type, using the corresponding supertype's schema and prompt. Results are collected and returned as a flat list of validated entity records.

### Interactive runner (`src/PoC/run_extraction.py`)

A step-by-step IPython script that exercises the full pipeline on the JSON files under a `data/<subdir>/` directory. Edit the config variables at the top of the file, then run it with:

```bash
ipython src/PoC/run_extraction.py
# or from an IPython/Jupyter session:
%run src/PoC/run_extraction.py
```

Key config variables:

| Variable | Default | Description |
|---|---|---|
| `DATA_SUBDIR` | `"legislative_gto"` | Subdirectory under `data/` to read (every `*.json` file is processed) |
| `FILES` | `None` | List of `Path` objects to process, or `None` for all files in the data dir |
| `MATCH_ONLY` | `False` | Skip LLM steps — only show keyword matches |
| `LIMIT` | `5` | Max records per file (`None` = no limit) |

The runner auto-detects two record shapes: Facebook-style posts with a nested `message` dict (e.g. `data/queretaro_fb_pages/`) and news-style flat docs with `text`, `title`, `url` top-level fields (e.g. `data/legislative_gto/` produced by `get_data.py`).

After execution, `all_entities` holds all validated entity dicts for further inspection.

### Fetching source data (`src/PoC/get_data.py`)

Helper script that runs an Elasticsearch search via the external `elastic_client` package and saves hits to `data/<subdir>/*.json` so the extraction pipeline can consume them offline.

```bash
python src/PoC/get_data.py                              # default: Ayuntamiento de Querétaro (entity_id=75)
GET_DATA_QUERY=legislative_gto python src/PoC/get_data.py   # legislative-initiatives Guanajuato query
GET_DATA_LIMIT=50 python src/PoC/get_data.py            # cap the result count
```

The module exposes `fetch_docs(request, fields=None, limit=None)` for arbitrary `FilterRequest`-shaped queries and `save_docs(docs, dest_dir)` for persisting the result. Two bundled queries are available, selected via `GET_DATA_QUERY`:

| `GET_DATA_QUERY` | Request | Output |
|---|---|---|
| `ayuntamiento_qro` (default) | `AYUNTAMIENTO_QRO_REQUEST` — last week of news tagged with KB `entity_id=75` ("Ayuntamiento de Querétaro") | `data/ayuntamiento_qro/ayuntamiento_qro_<timestamp>.json` |
| `legislative_gto` | `LEGISLATIVE_INITIATIVE_GTO_REQUEST` — last week of news matching `"guanajuato"` AND `"congreso"` AND any initiative-related keyword (`"iniciativa"`, `"reforma"`, `"decreto"`, …) | `data/legislative_gto/legislative_initiative_gto_<timestamp>.json` |

Requires `ELASTIC_HOST`, `ELASTIC_PORT`, `ELASTIC_AUTH`, `ELASTIC_HTTP_CERT` env vars and the `elastic_client` package importable (installed editable, or available under `/Users/oscarcuellar/ocn/media/elastic_client`).

### LLM Configuration

`call_llm()` uses the OpenRouter client at `src/llm/openrouter/`. Required environment variables:

| Variable | Required | Default | Description |
|---|---|---|---|
| `OPENROUTER_API_KEY` | yes | — | OpenRouter API key |
| `OPENROUTER_MODEL` | no | `openai/gpt-4o` | Model identifier (any model available on OpenRouter) |

The client uses the OpenAI-compatible chat completions endpoint with JSON mode enabled. To use a different model (e.g. `anthropic/claude-sonnet-4-20250514`, `google/gemini-pro`), set `OPENROUTER_MODEL`.

### Cache

Both classification and per-class extraction results are cached to `cache/` (project root) as JSON files, so re-processing the same article skips LLM calls for already-processed steps.

| Step | Key | Functions |
|------|-----|-----------|
| Classification | `sha256(classify\|article_url\|sorted_matched_classes)` | `_classify_cache_read()` / `_classify_cache_write()` |
| Extraction | `sha256(article_url\|class_name)` | `_cache_read()` / `_cache_write()` |

Classification cache is checked in `EntityExtractor.classify()` — the same article with the same set of candidate classes returns the cached confirmed list. Different candidate sets (e.g. from updated keyword rules) produce different cache keys and trigger a fresh LLM call.

Extraction cache is checked in `EntityExtractor.extract()` — the `extract_supertype()` method itself does not interact with the cache, so direct calls to it always hit the LLM.

Articles without a `url` or `id` field bypass both caches entirely.

## Design Guide

### Adding new classes

**New class under an existing supertype** — modify these files:

| File | Action |
|------|--------|
| `catalogues/event_types.csv` | Add row mapping the new class to its supertype |
| `catalogues/keywords.xlsx` | Add matching rules (keywords, phrases, filters) for the new class |
| `schemas/{supertype}.json` | Add the new class as an enum value in `event_type.enum` or `theme_type.enum` (if not already present) |
| `prompts/classes/{supertype}.txt` | Regenerate via `PromptGeneration().generate("{supertype}")` — the updated enum is picked up automatically |

**New supertype (new schema)** — create/modify these files:

| File | Action |
|------|--------|
| `schemas/{supertype}.json` | **Create**: define `meta.description`, `meta.example`, and all field definitions. The filename (without `.json`) is the supertype name, and the top-level JSON key must be the PascalCase version (e.g. `public_works_event` → `PublicWorksEvent`). See [Writing good schema descriptions](#writing-good-schema-descriptions) |
| `catalogues/event_types.csv` | Add rows mapping each class to the new supertype |
| `catalogues/keywords.xlsx` | Add matching rules (keywords, phrases, filters) for each class in the new supertype |
| `prompts/classes/{supertype}.txt` | **Generated**: run `PromptGeneration().generate("{supertype}")` |

For **theme supertypes**: same files and process. Theme schemas set `meta.category: "theme"`, use `theme_type` instead of `event_type`, have no required `date_range`, and use optional `location`. The `meta.description` should frame the theme as a broad classifier — any article that touches or discusses any related subject matches (events, complaints, mentions, statistics, policy). See existing theme schemas (e.g. `security.json`) for the pattern.

For **entity/concept supertypes**: same files and process. Entity schemas set `meta.category: "entity"`, use `entity_type` from the supertype's catalogue, have no required datetime, require `name`, and typically include a `jurisdiction` (Location) field. The `meta.description` should frame the entity as a specific, identifiable thing of this type — distinguishable from events (no specific occurrence date/place required) and from themes (not a broad topical classifier). `EntityExtractor.classify()` automatically routes entity candidates into the "Entidades/Conceptos" group via `meta.category`; no code change is needed to add a new entity supertype. See `legislative_initiative.json` for the pattern.

The extraction pipeline handles all ontology categories uniformly — the combination of `meta.category` (routing) and `meta.description` (classification prompt) drives the classification decision.

### Writing good schema descriptions

Schema descriptions are used by the prompt generator to craft extraction instructions and by the classification prompt to decide whether an article matches a class. Each layer of context matters:

- **`meta.category`**: one of `"event"`, `"theme"`, or `"entity"`. Required on every supertype schema. Drives routing in `EntityExtractor.classify()` — candidates are grouped by category in the LLM prompt with different selection criteria per group.
- **`meta.description`**: what the class represents and what distinguishes it from similar classes. For events, specify that it refers to identifiable single occurrences with location and date. For themes, frame as a broad classifier — list the subjects it covers and state that any article touching any related subject matches. For entities/concepts, frame as a specific, identifiable item of this type and state that only articles referring to a concrete, named or attribute-identified instance should match (not generic mentions of the domain or thematic discussion). This drives both the LLM classification step and the generated prompt's system message.
- **Field `description`**: what to extract and how. These become per-field extraction instructions in the generated prompt. Be specific about the domain (e.g. "Date or date range when the incident occurred" not just "Date").
- **Composite type `description`** (in `composite_types.json`): structural and behavioral instructions that are injected automatically for any field using that type. E.g. `precision_days` semantics, `mention` pattern for quoting original text.
- **`event_type.description`**: always include "Choose the single most specific category that matches."
- **`date_range.description`**: always specify what dates the field refers to (e.g. "when the incident occurred", "when the works are scheduled").
- **`meta.example` composite type completeness**: every composite type in the example must include ALL subfields from the type definition, with null for absent values. Omitting fields (e.g. showing only `country`, `state`, `city` for Location instead of all 8 fields) causes the generated prompt to miss those fields, and the extraction LLM will not extract them.

### Designing composite types

Composite types exist to make extracted information **machine-readable** rather than dumping everything into free-text strings. When designing a new composite type:

- **Decompose into typed fields**: break the information into its smallest useful parts with specific types (numbers, dates, enums, coordinates), not strings. For example, `PriceRange` separates `lower` (float), `upper` (float), and `currency` (str) instead of storing `"$200-$500 MXN"` as a single string.
- **Preserve the original text via `mention`**: most composite types include a `mention` field that captures the exact text as it appeared in the source ("texto tal cual se menciona en la nota"). This preserves the original phrasing for auditing and re-extraction while the structured fields hold the parsed values.
- **Include contextual metadata**: add fields for attributes that are implicit in human text but required for machine processing — `timezone` on dates, `currency` on prices, `precision_days` on approximate dates, `confidence_range` on estimates. Without these, downstream consumers must guess or assume.
- **Design for comparability**: fields should support direct comparison and aggregation across records. Two `DateRangeFromUnstructured` values can be compared programmatically; two free-text date strings cannot.

To register a new composite type: add it to `src/schema/types/composite_types.json` and `type_catalog.py`, then reference by name from any schema.
