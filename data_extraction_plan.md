# Data Extraction System — Implementation Plan

Production system for extracting structured entity/event data from unstructured text (news articles, social media) using LLM-based extraction guided by declarative schemas.

Replaces the legacy PoC in `src/PoC/events.py`.

---

## 1. System Overview

```
Article → Keyword Matching → Ontology Lookup → Schema Selection → Prompt Generation → LLM Extraction → Parse & Validate → Structured Output
```

**Core idea**: each entity/event type has a JSON schema (same format as `src/schema/`). The schema's field descriptions and type metadata are used to *auto-generate* LLM extraction prompts. When the schema changes, the prompt is regenerated.

### Data flow

1. **Ingest**: read articles from files (later: message queues)
2. **Match**: compare article keywords/categories against the ontology keyword catalogue
3. **Route**: map matched keywords → event types → supertypes → schemas
4. **Extract**: build prompt from schema, send article + prompt to LLM
5. **Parse**: feed LLM JSON response through the schema Parser (structure → types → defaults → validation)
6. **Output**: validated structured records

---

## 2. Ontology Design

### 2.1 Three-level hierarchy

```
keyword → event_type → supertype → schema
```

- **Keywords**: search terms that appear in articles or retrieval metadata (e.g. "concierto", "balacera", "bache")
- **Event types** (classes): specific entity categories (e.g. `concert`, `shooting`, `pothole`). Each maps to exactly one supertype.
- **Supertypes** (superclasses): groups of related event types that share the same extraction schema. One supertype = one schema = one extraction prompt.

### 2.2 Supertypes and their event types

| Supertype | Event types (classes) | Description |
|---|---|---|
| `paid_mass_event` | concert, festival, party, fair, inauguration, sports_event, religious_event, cultural_event, congress, exposition, conference, convention | Events with attendance, venue, pricing, dates |
| `robbery_assault` | robbery, assault, security_event | Property/person crimes |
| `public_works` | pothole, street_lighting, paving, public_transport, infrastructure | Non-event location-based infrastructure issues |
| `violence_event` | shooting, attack, homicide, confrontation | Violent incidents |
| `closures_interruptions` | blockade, closure, suspension_of_operations | Traffic/service disruptions |
| `emergency` | fire, crash, explosion, flood, accident, emergency_general | Emergency incidents |
| `arrest` | arrest, detention | Law enforcement captures |

### 2.3 Catalogue storage

Two CSV files in `src/entities/extraction/catalogues/`:

**`event_types.csv`** — maps event types to supertypes with Spanish labels:
```
event_type,supertype,label_es,label_en
concert,paid_mass_event,Concierto,Concert
shooting,violence_event,Balacera,Shooting
...
```

**`keywords.csv`** — maps search keywords to event types:
```
keyword,event_type,match_type
concierto,concert,exact
festival,festival,exact
balazo,shooting,stem
...
```

`match_type` supports: `exact` (literal match), `stem` (stemmed match), `category` (matches a news category rather than a keyword).

---

## 3. Schema Design

### 3.1 Schema location

Entity extraction schemas live in `src/entities/extraction/schemas/*.json`, separate from the pipeline schemas in `src/schema/schemas/`. They use the same JSON format and are loaded with the same `load_schema()` infrastructure.

### 3.2 Shared composite types

New composite types added to `src/schema/types/composite_types.json` (reusable across all schemas):

| Composite type | Fields | Description |
|---|---|---|
| `Location` | country, state, city, neighborhood, zone, street, number, place_name | Structured location extracted from text |
| `PriceRange` | mention, lower, upper, currency | Price/cost range with original mention |
| `Attendance` | mention, estimate | Estimated attendance with original mention |
| `VenueCapacity` | mention, capacity | Venue capacity with original mention |

Existing composite types reused: `DateRangeFromUnstructured`, `LocationCoords`, `PeriodDates`.

### 3.3 Schema-to-prompt mapping

Each schema field has a `"description"` key that serves as the extraction instruction for the LLM. The prompt generator reads the schema JSON and assembles:

1. **System message**: role definition (structured information extractor)
2. **Task description**: auto-generated from schema `meta.description` + field descriptions
3. **Field instructions**: one numbered section per field, derived from:
   - Field name and description
   - Type information (enum values become a catalogue in the prompt)
   - Example values from `meta.example` if present
4. **Response format**: JSON structure matching the schema
5. **Article text**: the input document

### 3.4 Schemas to implement now

Seven schemas, one per supertype:

1. **`paid_mass_event.json`** — the most detailed, based on the legacy `events.py` prompt
2. **`robbery_assault.json`** — crime-specific fields (victim_count, weapon, stolen_items)
3. **`public_works.json`** — infrastructure fields (status, responsible_authority, affected_area)
4. **`violence_event.json`** — violence-specific fields (victim_count, weapon, perpetrator)
5. **`closures_interruptions.json`** — disruption fields (affected_routes, cause, duration)
6. **`emergency.json`** — emergency fields (casualties, damage, response_agencies)
7. **`arrest.json`** — arrest fields (detainee, charges, authority)

All schemas share common fields (defined via composite types): location, date_range, name, description, tags, context, relevance, event_type, event_subtype, status.

---

## 4. Prompt Generation

### 4.1 Generator module

`src/entities/extraction/prompt_generator.py`

```python
def generate_prompt(schema_path: str, context: dict) -> list[dict]:
    """Generate LLM messages from a schema JSON file.
    
    Args:
        schema_path: path to the entity schema JSON
        context: variables like current_date, article_text
    
    Returns:
        List of message dicts [{"role": "system", ...}, {"role": "user", ...}]
    """
```

The generator:
1. Loads the schema JSON (raw, not resolved — we need the descriptions)
2. Iterates fields, building numbered instructions from descriptions
3. Inserts enum values as catalogues
4. Adds example JSON from `meta.example`
5. Injects context variables (date, article text)
6. Saves the generated prompt to `src/entities/extraction/prompts/{schema_name}.txt` for caching

### 4.2 Prompt caching

Generated prompts are saved to `src/entities/extraction/prompts/`. A prompt is regenerated only when:
- The schema file's modification time is newer than the cached prompt
- Explicitly requested

### 4.3 Feature subset extraction

For large schemas, the system can split extraction into multiple LLM calls, each extracting a subset of fields. The challenge is joining results when one article produces multiple entities.

**Join strategy**: 
1. First call always extracts **anchor fields**: `name`, `event_type`, `event_subtype`, `date_range`, `location` — enough to uniquely identify each entity
2. Subsequent calls extract remaining field subsets, re-sending anchor fields as context so the LLM can match features to the correct entity
3. Results are joined by matching on anchor fields (name + type + date fuzzy match)

This is a later optimization — initial implementation extracts all fields in one call.

---

## 5. Extraction Pipeline

### 5.1 Module structure

```
src/entities/
  extraction/
    __init__.py
    schemas/              # Entity JSON schemas
      paid_mass_event.json
      robbery_assault.json
      public_works.json
      violence_event.json
      closures_interruptions.json
      emergency.json
      arrest.json
    catalogues/           # Ontology CSV files
      event_types.csv
      keywords.csv
    prompts/              # Cached generated prompts
    prompt_generator.py   # Schema → prompt
    extractor.py          # Orchestrates: match → route → extract → parse
    ontology.py           # Loads catalogues, keyword→type→supertype lookups
  linking/                # (future) Entity linking/mapping
  readme_entities.md
```

### 5.2 Extractor pipeline

`src/entities/extraction/extractor.py`:

```python
class EntityExtractor:
    def __init__(self, schema_dir, catalogue_dir, prompt_dir):
        self.ontology = Ontology(catalogue_dir)
        self.schemas = self._load_schemas(schema_dir)
        self.prompt_cache = prompt_dir
    
    def extract(self, article: dict) -> list[dict]:
        """Extract structured entities from an article.
        
        1. Match article keywords/categories to event types
        2. Determine supertypes (schemas) to apply
        3. For each supertype, generate/load prompt and call LLM
        4. Parse and validate each response
        5. Return list of validated entity records
        """
```

### 5.3 Data retrieval

For now, articles are read from JSON/JSONL files or DataFrames. The retrieval interface:

```python
class ArticleSource:
    def __iter__(self) -> Iterator[dict]:
        """Yield article dicts with at least: id, text, title, date, keywords."""
```

File-based implementation reads from `data/` directory. Later replaced with message queue consumer (same interface).

---

## 6. Implementation Phases

### Phase 1 — Schema & Ontology (current)
- [x] Design composite types (Location, PriceRange, Attendance, VenueCapacity)
- [x] Write all 7 entity schemas in JSON
- [x] Write ontology catalogues (event_types.csv, keywords.csv)
- [x] Document in readme_entities.md

### Phase 2 — Prompt Generation
- [ ] Implement `prompt_generator.py` — reads schema JSON, produces LLM messages
- [ ] Implement prompt caching (save to prompts/, check mtime)
- [ ] Test: generate prompts for all 7 schemas, verify they match expected format

### Phase 3 — Ontology Module
- [ ] Implement `ontology.py` — load CSVs, keyword matching, type→supertype→schema lookups
- [ ] Support keyword matching: exact, stemmed, category-based
- [ ] Test with sample articles and keyword lists

### Phase 4 — Extraction Pipeline
- [ ] Implement `extractor.py` — orchestrator class
- [ ] Implement `ArticleSource` — file-based reader
- [ ] Wire: article → ontology match → prompt selection → LLM call → response parsing
- [ ] Use existing `Parser` + `load_schema()` for response validation
- [ ] Test end-to-end with sample articles

### Phase 5 — Feature Subset Extraction (optimization)
- [ ] Split large schemas into field groups
- [ ] Implement anchor-field extraction + secondary calls
- [ ] Implement join logic (fuzzy match on name + type + date)

### Phase 6 — Production Readiness
- [ ] Replace file reader with message queue consumer
- [ ] Add batch/async LLM support (OpenAI Batch API)
- [ ] Error handling, retries, logging
- [ ] Monitoring and metrics

---

## 7. Key Design Decisions

1. **One schema per supertype, not per event type**: keeps the number of schemas manageable. The `event_type` field within each schema uses an enum to distinguish subtypes.

2. **Prompt generated from schema, not hand-written**: ensures prompt and schema stay in sync. Field descriptions serve double duty as schema documentation and LLM instructions.

3. **Same schema infrastructure as pipeline schemas**: entity extraction schemas use the same JSON format, `load_schema()`, `Parser`, composite types, and type registry. No parallel system.

4. **Ontology in CSV, not code**: easy to edit, extend, and eventually move to a database. The code just loads and indexes the CSVs.

5. **Keywords map to types, not directly to schemas**: the keyword→type→supertype→schema chain allows multiple keywords to map to the same extraction, and event types to be reorganized across supertypes without changing keyword mappings.

6. **English field names, Spanish prompts**: schema field names and code are in English. The prompt generator produces Spanish-language instructions (configurable) because the source material is Spanish.
