# Entities — Extraction & Mapping

This directory implements the entity/event extraction pipeline: structured data extraction from unstructured text using LLM-based extraction guided by declarative JSON schemas.

## Directory Structure

```
entities/
  extraction/
    schemas/              # Entity JSON schemas (one per supertype)
      paid_mass_event.json
      robbery_assault.json
      public_works.json
      violence_event.json
      closures_interruptions.json
      emergency.json
      protest.json
      arrest.json
    catalogues/           # Ontology catalogues
      event_types.csv     # event_type → supertype mapping with labels
      keywords.xlsx       # Matching rules: class, keywords, filters (Excel)
      keywords.csv        # (legacy reference — not used by the system)
    prompts/
      classes/            # Generated extraction prompts (one per supertype, .txt)
    extract.py            # Extraction pipeline: matching, LLM calls, parsing
    prompt_generator.py   # Schema → LLM prompt auto-generation
  linking/                # (future) Entity linking/deduplication
  readme_entities.md      # This file
```

## Ontology

Three-level hierarchy for routing articles to the correct extraction schema:

```
keyword → event_type → supertype → schema
```

### Event types (`catalogues/event_types.csv`)

Each event type maps to exactly one **supertype** (superclass). The supertype determines which JSON schema is used for extraction.

| Supertype | Event types | Schema |
|---|---|---|
| `paid_mass_event` | concert, festival, party, fair, inauguration, sports_event, religious_event, cultural_event, congress, exposition, conference, convention | `paid_mass_event.json` |
| `robbery_assault` | robbery, assault, kidnapping, security_event | `robbery_assault.json` |
| `public_works` | pothole, street_lighting, paving, public_transport, infrastructure, trash_complaint, water_issue, sinkhole, public_road | `public_works.json` |
| `violence_event` | shooting, attack, homicide, confrontation | `violence_event.json` |
| `closures_interruptions` | blockade, closure, suspension_of_operations | `closures_interruptions.json` |
| `emergency` | fire, crash, explosion, flood, accident, pedestrian_hit, emergency_general | `emergency.json` |
| `protest` | protest | `protest.json` |
| `arrest` | arrest, detention | `arrest.json` |

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

Entity schemas live in `extraction/schemas/` and use the same JSON format as the pipeline schemas in `src/schema/schemas/`. They are loaded with the same `load_schema()` infrastructure.

Each schema defines:
- **`meta.description`**: describes what this entity type represents. Used in two LLM-facing stages: (1) the classification step, where it helps the LLM decide whether an article actually refers to this event type, and (2) prompt generation, where it becomes the LLM system message. Write these descriptions carefully — they directly affect classification accuracy and extraction quality.
- **`meta.example`**: a complete example of the expected JSON output (included in the generated prompt)
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

### Common fields

All schemas share these fields (with the same semantics):
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

## Prompt Generation (`prompt_generator.py`)

Auto-generates Spanish-language extraction prompts from JSON schemas using a two-step LLM process (generate + feedback/revision).

### Context assembly (`PromptGenerationContextManager`)

For a given supertype, gathers three layers of context into a structured dict that the generation LLM uses to craft the prompt:

1. **Class-level**: `meta.description` (what this entity type represents — drives both the LLM classification step and the generated prompt's system message) and `meta.example` (complete JSON output example included in the prompt)
2. **Field-level**: each field's `description`, `type`, `required`, `enum` values — these become per-field extraction instructions in the generated prompt
3. **Composite type-level**: for fields referencing composite types (e.g. `DateRangeFromUnstructured`, `Location`, `PriceRange`), the type's `meta.description` and per-field descriptions are included — these contribute structural instructions (e.g. approximate date handling with `precision_days`, the `mention` pattern for quoting original text, `Location.place_name` guidance)

### Generation template

A prompt sent to a generation LLM that:
- Receives the schema context + `robbery_assault.txt` as reference style exemplar
- Translates all English descriptions to instructional Spanish
- Renders `EnumStr` fields as catalogues with `"value" — Spanish label: description`
- Expands composite types with JSON structure examples and `mention` pattern ("texto tal cual se menciona en la nota")
- Injects global rules: null for missing fields, don't invent events, JSON list response format
- Injects type-specific rules: `DateRangeFromUnstructured`/`DateFromUnstructured` approximate date instructions and `precision_days` examples, `Location` place_name guidance
- Includes template variables `{date_now}`, `{source_type}`, `{body}` for runtime substitution

### Feedback loop

The generated draft is sent to a separate feedback LLM (potentially a different, more powerful model) that checks completeness, schema consistency, Spanish quality, format, and template variables. Feedback is then applied by the generation LLM to produce the final prompt.

### Output

Saved to `prompts/classes/{supertype}.txt` in `SYSTEM:/USER:/USER:` format, ready for `_load_prompt()` to load at extraction time.

### Runtime context variables

Generated prompts include these template variables, substituted by `extract.py` at runtime:

| Variable | Source | Description |
|---|---|---|
| `{date_now}` | `datetime.now()` | Current date (dd/mm/YYYY), used as temporal context for date interpretation |
| `{source_type}` | `article["source_type"]` | Source type (e.g. "news", "facebook"). For social media, dates can be inferred from the publication date |
| `{body}` | `article["text"]` | The article text to extract from |

### Usage

```python
from src.entities.extraction.prompt_generator import PromptGeneration

gen = PromptGeneration()
gen.generate("emergency")     # generate one supertype
gen.generate_all()            # generate all supertypes
```

### LLM configuration

| Variable | Required | Default | Description |
|---|---|---|---|
| `OPENROUTER_GENERATION_MODEL` | no | `anthropic/claude-opus-4.6` | Model for prompt generation |
| `OPENROUTER_FEEDBACK_MODEL` | no | `openai/gpt-5.4` | Model for prompt feedback/review |

### Writing good schema descriptions

When adding or editing schemas, keep in mind that the prompt generator uses these descriptions to craft extraction instructions. Each layer of context matters:

- **`meta.description`**: what the entity type represents and what distinguishes it from similar types. This drives both the LLM classification step and the generated prompt's system message.
- **Field `description`**: what to extract and how. These become per-field extraction instructions in the generated prompt. Be specific about the domain (e.g. "Date or date range when the incident occurred" not just "Date").
- **Composite type `description`** (in `composite_types.json`): structural and behavioral instructions that are injected automatically for any field using that type. E.g. `precision_days` semantics, `mention` pattern for quoting original text.
- **`event_type.description`**: always include "Choose the single most specific category that matches."
- **`date_range.description`**: always specify what dates the field refers to (e.g. "when the incident occurred", "when the works are scheduled").

## Extraction Pipeline (`extract.py`)

```
Article → rule matching → LLM classification → per-class LLM extraction → JSON parse → schema validation → structured output
```

### Three-step flow

1. **Keyword matching** — `Ontology.match()` evaluates all keyword/phrase/category rules against the article, returning a set of candidate ontology classes (event_types).
2. **LLM classification** — `EntityExtractor.classify()` presents the LLM with the article and the subset of ontology classes that matched (with their descriptions). The LLM decides which classes the article genuinely discusses, filtering out false positives from keyword overlap.
3. **Per-class extraction** — For each confirmed class, `EntityExtractor.extract_supertype()` loads the class's supertype schema/prompt and calls the LLM with a focus instruction scoping extraction to that specific event type. Results are parsed and validated.

This flow avoids redundant extraction calls for keyword matches that don't correspond to actual content, and produces cleaner results when an article triggers keywords from multiple unrelated classes.

### Components

- **`Ontology`** — loads `event_types.csv` and `keywords.xlsx`, evaluates matching rules (keywords, exclusions, categories, document type) against articles, resolves matched ontology classes to supertypes, and provides class descriptions for the classification step.
- **`EntityExtractor`** — orchestrates the pipeline: `match()` finds candidate classes, `classify()` asks the LLM which classes actually apply, `extract_supertype()` extracts events scoped to a specific class, `extract()` runs the full three-step flow.
- **`call_llm()`** — sends messages to an LLM via OpenRouter (`src/llm/openrouter/`). Requests JSON mode for reliable parsing. Model and API key configured via environment variables (see below).
- **`_load_prompt()`** — reads prompt files from `prompts/classes/`, substitutes context variables (`{date_now}`, `{source_type}`, `{body}`).
- **`_validate_entity()`** — runs each extracted entity through the schema `Parser` for type coercion and validation.

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

A step-by-step IPython script that exercises the full pipeline on the Facebook posts in `data/queretaro_fb_pages/`. Edit the config variables at the top of the file, then run it with:

```bash
ipython src/PoC/run_extraction.py
# or from an IPython/Jupyter session:
%run src/PoC/run_extraction.py
```

Key config variables:

| Variable | Default | Description |
|---|---|---|
| `FILES` | `None` | List of `Path` objects to process, or `None` for all files in the data dir |
| `MATCH_ONLY` | `False` | Skip LLM steps — only show keyword matches |
| `LIMIT` | `5` | Max posts per file (`None` = no limit) |

After execution, `all_entities` holds all validated entity dicts for further inspection.

### LLM Configuration

`call_llm()` uses the OpenRouter client at `src/llm/openrouter/`. Required environment variables:

| Variable | Required | Default | Description |
|---|---|---|---|
| `OPENROUTER_API_KEY` | yes | — | OpenRouter API key |
| `OPENROUTER_MODEL` | no | `openai/gpt-4o` | Model identifier (any model available on OpenRouter) |

The client uses the OpenAI-compatible chat completions endpoint with JSON mode enabled. To use a different model (e.g. `anthropic/claude-sonnet-4-20250514`, `google/gemini-pro`), set `OPENROUTER_MODEL`.

## Adding New Entity Types

1. **New event type under existing supertype**: add a row to `event_types.csv` and add matching rules to `keywords.xlsx`. If the new type needs a new enum value, add it to the schema's `event_type.enum` list.

2. **New supertype (new schema)**: create a new JSON schema in `schemas/`, add event types to `event_types.csv`, add matching rules to `keywords.xlsx`. The prompt generator will auto-generate the extraction prompt.

3. **New composite type**: add to `src/schema/types/composite_types.json` and `type_catalog.py`, then reference by name from any schema.
