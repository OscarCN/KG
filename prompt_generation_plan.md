# Prompt Generation System — Implementation Plan

## Background & Motivation

The entity extraction pipeline (`src/entities/extraction/extract.py`) extracts structured events and themes from news articles and social media using LLMs. It has **15 supertypes** — 8 events (paid_mass_event, robbery_assault_event, public_works_event, violence_event, closures_interruptions_event, emergency_event, protest_event, arrest_event) and 7 themes (security, public_infrastructure, civil_protection, mobility, culture, sports, civic_participation) — each with a JSON schema defining fields, types, enums, and descriptions.

The pipeline loads Spanish-language prompt `.txt` files from `prompts/classes/{supertype}.txt`, substitutes runtime context variables (`{date_now}`, `{body}`), and sends them to an LLM via OpenRouter for extraction. Currently **only `robbery_assault` has a hand-written prompt** — the other 7 supertypes have no prompts, which means extraction fails with `FileNotFoundError` for them.

We need a system that **auto-generates Spanish extraction prompts from the JSON schemas** using LLMs, following the style of the existing `paid_mass_event.txt` and the original PoC prompt in `src/PoC/events.py`.

## Key Architecture

### How extraction works today

1. `extract.py` → `_load_prompt(supertype, context)` reads `prompts/classes/{supertype}.txt`
2. Prompt format: `SYSTEM:\n<text>\nUSER:\n<text>\nUSER:\n<text>` with `{date_now}`, `{body}` placeholders
3. `call_llm()` sends messages to OpenRouter (JSON mode, model from `OPENROUTER_MODEL` env var)
4. Response parsed as JSON list of entity dicts, validated through schema `Parser`

### How prompt generation will work

1. `PromptGenerationContextManager` gathers schema context (class description, field definitions, composite type definitions) into a structured dict
2. `PromptGeneration` sends context + reference prompt + generation template to a **generation LLM**
3. Draft sent to a **feedback LLM** (different, more powerful model) for review
4. Feedback applied, final prompt saved to `prompts/classes/{supertype}.txt`

### Key files to understand before implementing

| File | Purpose |
|---|---|
| `src/entities/extraction/extract.py` | Extraction pipeline — loads prompts, calls LLM, parses results. `_load_prompt()` at line 305, `extract_supertype()` at line 538. **Must be updated**: prompt path (line 313) and context dict (line 561) |
| `src/entities/extraction/prompts/classes/paid_mass_event.txt` | **Reference prompt** — 174 lines, Spanish, `SYSTEM:/USER:/USER:` format. This is the style exemplar for generated prompts |
| `src/PoC/events.py` | Original PoC extraction prompt (lines 231-445) — detailed Spanish instructions, the style we want to replicate |
| `src/entities/extraction/schemas/*.json` | 8 schema JSONs. Each has `meta.description`, `meta.example`, and `schema` with field definitions |
| `src/schema/types/composite_types.json` | Composite type definitions (Location, DateRangeFromUnstructured, PriceRange, etc.) with meta descriptions and per-field descriptions |
| `src/schema/types/composite_types.py` | Loads composite types: `COMPOSITE_TYPES` (resolved schemas), `COMPOSITE_META` (meta dicts) |
| `src/schema/schemas/read_schema.py` | `load_schema()` — loads JSON schemas, resolves composite type dependencies |
| `src/llm/openrouter/client.py` | `OpenRouterClient` with per-call `model` override. `call_openrouter(messages, model=..., temperature=..., max_tokens=...)` |

### Supertype → schema key mapping (from extract.py)

```
## Events
paid_mass_event              → PaidMassEvent
robbery_assault_event        → RobberyAssaultEvent
public_works_event           → PublicWorksEvent
violence_event               → ViolenceEvent
closures_interruptions_event → ClosuresInterruptionsEvent
emergency_event              → EmergencyEvent
protest_event                → ProtestEvent
arrest_event                 → ArrestEvent
## Themes
security                     → Security
public_infrastructure        → PublicInfrastructure
civil_protection             → CivilProtection
mobility                     → Mobility
culture                      → Culture
sports                       → Sports
civic_participation          → CivicParticipation
```

---

## Step 1: Schema & Composite Type Fixes

These are prerequisites — the prompt generator reads these descriptions to build context.

### 1a. New composite type `DateFromUnstructured` in `src/schema/types/composite_types.json`

For single-date fields (like `public_works.completion_date`) that don't need a range. Add after `DateRangeFromUnstructured`:

```json
"DateFromUnstructured": {
    "meta": {
        "description": "Represents a single date extracted from unstructured text, with the parsed datetime, the original unstructured mention, and the precision confidence period in days"
    },
    "schema": {
        "date":           {"type": "datetime", "description": "The parsed date in YYYY-MM-DDTHH:MM:SS format"},
        "mention":        {"type": "str", "description": "El texto tal cual se menciona en la nota. The original text mention of the date, exactly as it appears in the source."},
        "precision_days": {"type": "int", "description": "When the mention is not an exact date, a reasonable range in days within which the real date should fall. If only an approximate period is mentioned (e.g. 'next week', or 'in March'), estimate a period in days in which, from the start date, the actual date is likely to fall (e.g. 'la proxima semana' -> precision_days=7). Null when the date is exact."}
    }
}
```

### 1b. Improve composite type descriptions in `src/schema/types/composite_types.json`

- **`DateRangeFromUnstructured.meta.description`**: append: "If only an approximate period is mentioned (e.g. 'next week', or 'in March'), estimate a period in days in which, from the start date, the actual event date is likely to fall (e.g. 'la semana pasada' -> precision_days=7)."
- **`DateRangeFromUnstructured.mention`**: add description field: `"El texto tal cual se menciona en la nota. The original text mention of the date range, exactly as it appears in the source."`
- **All `mention` fields** across PriceRange, Attendance, VenueCapacity, CasualtyCount, CountMention: prepend `"El texto tal cual se menciona en la nota. "` to existing descriptions
- **`Location.meta.description`**: already has "Each field should only be filled if explicitly mentioned" — no change needed

### 1c. Fix `event_type.description` in all 8 schemas under `src/entities/extraction/schemas/`

Add "Choose the single most specific category that matches." Currently only `paid_mass_event` has this. Apply to the other 7:
- `robbery_assault_event.json`: "Type of crime from the catalogue." → "Type of crime from the catalogue. Choose the single most specific category that matches."
- Same pattern for: `public_works_event`, `violence_event`, `closures_interruptions_event`, `emergency_event`, `protest_event`, `arrest_event`

### 1d. Fix `date_range.description` in 7 schemas (paid_mass_event already good)

Make each domain-specific. Pattern: "Date or date range when [specific thing]. Extract the start datetime and, if present, the end datetime."

- `robbery_assault_event`: "Date or date range when the incident occurred. Extract the start datetime and, if present, the end datetime."
- `public_works_event`: "Date or date range when the issue was reported or the works started/are scheduled. Extract the start datetime and, if present, the end datetime."
- `violence_event`: "Date or date range when the violent incident occurred. Extract the start datetime and, if present, the end datetime."
- `closures_interruptions_event`: "Date or date range of the closure or interruption. Extract the start datetime and, if present, the end datetime."
- `emergency_event`: "Date or date range of the emergency. Extract the start datetime and, if present, the end datetime."
- `protest_event`: "Date or date range of the protest. Extract the start datetime and, if present, the end datetime."
- `arrest_event`: "Date or date range of the arrest or detention. Extract the start datetime and, if present, the end datetime."

### 1e. Fix `public_works.json`

- Change `completion_date.type` from `DateRangeFromUnstructured` → `DateFromUnstructured`
- Update description: "Expected completion date for the works project, if mentioned."
- Update `meta.example.completion_date` to match new structure:
  ```json
  "completion_date": {
      "date": "2025-10-15T00:00:00",
      "mention": "la proxima semana",
      "precision_days": 7
  }
  ```

---

## Step 2: New file `src/entities/extraction/prompt_generator.py`

### 2a. `PromptGenerationContextManager`

Gathers schema context for a supertype into a structured dict suitable for the generation LLM.

```python
class PromptGenerationContextManager:
    def __init__(self, supertype: str)
    def _load_raw_schema(self) -> dict        # raw JSON — keeps type names as strings
    def _load_raw_composite_types(self) -> dict  # raw JSON from composite_types.json
    def _build_context(self) -> dict
    def _gather_fields(self, schema) -> list   # [{name, type, description, required, enum}]
    def _gather_composite_types(self, schema) -> dict  # {type_name: {meta_description, fields}}
    def to_dict(self) -> dict
    def to_json(self) -> str
```

**Output structure:**
```json
{
  "supertype": "robbery_assault",
  "schema_key": "RobberyAssault",
  "meta_description": "A robbery, assault, or property/person crime incident...",
  "meta_example": { "event_type": "robbery", "event_subtype": "carjacking", ... },
  "fields": [
    {"name": "event_type", "type": "EnumStr", "description": "...", "required": true, "enum": ["robbery", "assault", ...]},
    {"name": "date_range", "type": "DateRangeFromUnstructured", "description": "...", "required": false, "enum": null},
    {"name": "location", "type": "Location", "description": "...", "required": false, "enum": null}
  ],
  "composite_types": {
    "DateRangeFromUnstructured": {
      "meta_description": "Represents a date period extracted from unstructured text...",
      "fields": [
        {"name": "date_range", "type": "PeriodDates", "description": null},
        {"name": "timezone", "type": "str", "description": null},
        {"name": "mention", "type": "str", "description": "El texto tal cual..."},
        {"name": "precision_days", "type": "int", "description": "When the mention is not an exact date..."}
      ]
    },
    "PeriodDates": { "meta_description": "...", "fields": [...] },
    "Location": { "meta_description": "...", "fields": [...] }
  },
  "raw_schema_json": "{ full JSON string }"
}
```

**Key implementation notes:**
- Read raw JSON directly (not through `load_schema()`) so type names stay as readable strings like `"DateRangeFromUnstructured"`, not resolved Python types
- Composite type resolution: recursively gather referenced types (e.g. `DateRangeFromUnstructured` references `PeriodDates`)
- Detect `List[X]` type patterns to resolve the inner type (e.g. `List[DateRangeFromUnstructured]`)
- Supertypes are discovered dynamically from schema files in the schemas directory; the PascalCase schema key is derived from the snake_case supertype name via `_snake_to_pascal()`

### 2b. `PromptGeneration`

```python
class PromptGeneration:
    GENERATION_MODEL_ENV = "OPENROUTER_GENERATION_MODEL"
    FEEDBACK_MODEL_ENV = "OPENROUTER_FEEDBACK_MODEL"
    DEFAULT_MODEL = "anthropic/claude-sonnet-4-20250514"

    def __init__(self):
        self.generation_model = os.environ.get(self.GENERATION_MODEL_ENV, self.DEFAULT_MODEL)
        self.feedback_model = os.environ.get(self.FEEDBACK_MODEL_ENV, self.DEFAULT_MODEL)

    def generate(self, supertype: str) -> str    # full pipeline: context → draft → feedback → final → save
    def _generate_draft(self, context: dict) -> str
    def _get_feedback(self, draft: str, context: dict) -> str
    def _apply_feedback(self, draft: str, feedback: str, context: dict) -> str
    def _validate_prompt(self, prompt_text: str) -> list[str]  # returns list of warnings
    def _save_prompt(self, supertype: str, prompt_text: str)
    def generate_all(self)                       # all 8 supertypes
```

**LLM calls** via `call_openrouter(messages, model=..., max_tokens=8192, temperature=0.3)`. **No JSON mode** — output is free-form text (the prompt), not JSON.

**Reference prompt** loaded at runtime from `src/entities/extraction/prompts/classes/paid_mass_event.txt`.

#### Generation template (constant string in the file)

System message to generation LLM:
> "You are an expert at writing structured extraction prompts for LLMs. You will receive a schema definition for an entity type and a reference example of a well-written extraction prompt. Your task is to generate a new extraction prompt in Spanish following the same style and conventions."

User message contains:
1. **Schema context JSON** from `PromptGenerationContextManager.to_json()`
2. **Reference prompt** — full text of `paid_mass_event.txt`
3. **Detailed instructions** (this is critical):
   - Output must use `SYSTEM:\n...\nUSER:\n...\nUSER:` format
   - **Translate everything to Spanish** — descriptions, class names, field labels
   - **Make descriptions instructional** — use context from three layers: class `meta.description`, field `description`, and composite type `meta.description`/field descriptions
   - **SYSTEM section**: "Eres un modelo para extraer información estructurada de [domain] en artículos de noticias." Include note that `{source_type}` provides article source context, and for social media posts, dates can often be inferred solely from the publication date (`{date_now}`) if not explicitly mentioned
   - **First USER section**:
     - Domain intro derived from `meta.description`
     - Global rules: "Si no se menciona un campo, deja su valor null. No inventes [events/incidents], solo extrae lo que esté explícitamente relacionado en la nota."
     - Numbered fields, for each:
       - **EnumStr**: catalogue with `"value" — Spanish label: description`
       - **Composite types** (DateRangeFromUnstructured, Location, PriceRange, etc.): expand structure with JSON examples, explain `mention` pattern ("Responde con el texto tal cual se menciona en la nota")
       - **DateRangeFromUnstructured/DateFromUnstructured**: approximate date instructions, `precision_days` examples, date format "YYYY-MM-DDTHH:MM:SS", inject `{date_now}` context
       - **Location**: "Llena solo los campos mencionados explícitamente en la nota" + `place_name` guidance (only proper names of identifiable places, no generic descriptions)
       - **List[str]**: show example array
       - **bool**: explain true/false/null semantics
     - End with "Formato de respuesta:" + complete JSON example from `meta.example` + "No añadas texto adicional fuera del JSON."
   - **Last USER section**: `"La noticia es la siguiente:\n\n{body}"`
   - **Template variables**: `{date_now}`, `{source_type}`, `{body}` must appear as **literal placeholders** (not substituted)
   - Output only the prompt text, no explanation or commentary

#### Feedback template

Sends draft + schema context + reference to a **different LLM** (feedback model). Checks:
1. **Completeness**: every field in the schema is covered in the prompt
2. **Consistency**: field names, types, enum values match the schema exactly
3. **Spanish quality**: natural instructional Spanish, not awkward translations
4. **Format**: follows `SYSTEM:/USER:/USER:` pattern correctly
5. **Template variables**: `{date_now}`, `{source_type}`, `{body}` all present
6. **Example JSON**: matches the schema's `meta.example` structure

Returns structured feedback as numbered issues.

#### Apply feedback

Sends draft + feedback + context back to generation LLM to fix listed issues and return the corrected prompt. Single additional LLM call.

#### Validation

After final generation, check:
- Contains `SYSTEM:` and at least one `USER:`
- Contains `{body}`, `{date_now}`
- Warn (don't fail) on issues — prompt can be manually refined

#### Save

To `src/entities/extraction/prompts/classes/{supertype}.txt`, creating directories with `Path.mkdir(parents=True, exist_ok=True)`.

---

## Step 3: Update `src/entities/extraction/extract.py`

### 3a. Fix prompt path (line 313)

```python
# Before:
path = _PROMPTS_DIR / f"{supertype}.txt"
# After:
path = _PROMPTS_DIR / "classes" / f"{supertype}.txt"
```

### 3b. Add `{source_type}` to context (line 561)

```python
source_type = article.get("source_type", "news")
context = {"date_now": date_now, "body": body, "source_type": source_type}
```

---

## Step 4: Documentation

### `README.md` (reference-level)

- Update directory structure to include `prompt_generator.py`
- In "Entity Extraction" section add brief note:
  - Schema-driven prompt generation: prompts auto-generated from JSON schemas via LLM, using a generate+feedback loop
  - Context injection: each prompt is built from three layers — class `meta.description`, field `description`, and composite type descriptions (e.g. `DateRangeFromUnstructured` contributes approximate-date and `precision_days` instructions)
  - Reference: `paid_mass_event.txt` serves as the style exemplar for all generated prompts
- Document `DateFromUnstructured` composite type
- Document env vars: `OPENROUTER_GENERATION_MODEL`, `OPENROUTER_FEEDBACK_MODEL`

### `src/entities/readme_entities.md` (detailed)

- Update directory structure (add `prompt_generator.py`, note `prompts/classes/` is generated output)
- Replace "Prompt Generation (Phase 2)" section — no longer "(future)". New content:

  **Prompt Generation (`prompt_generator.py`)**

  - **Context assembly** (`PromptGenerationContextManager`): For a given supertype, gathers three layers of context:
    1. **Class-level**: `meta.description` (what this entity type represents) and `meta.example` (complete JSON output example)
    2. **Field-level**: each field's `description`, `type`, `required`, `enum` values — these become per-field extraction instructions
    3. **Composite type-level**: for fields referencing composite types (e.g. `DateRangeFromUnstructured`, `Location`, `PriceRange`), the type's `meta.description` and per-field descriptions are included — these contribute structural instructions (e.g. approximate date handling, `mention` pattern, `precision_days` semantics)
  - **Generation template**: prompt sent to a generation LLM that receives schema context + `paid_mass_event.txt` as reference style. Translates English descriptions to instructional Spanish, renders EnumStr as catalogues, expands composite types with JSON examples, injects global rules (null for missing fields, don't invent events, JSON list format), injects type-specific rules (date approximation, Location guidance, mention pattern). Includes `{date_now}`, `{source_type}`, `{body}` as template variables.
  - **Feedback loop**: draft is sent to a separate feedback LLM for review (completeness, schema consistency, Spanish quality, format), then feedback is applied to produce the final prompt
  - **Output**: saved to `prompts/classes/{supertype}.txt` in `SYSTEM:/USER:/USER:` format

  **Writing good schema descriptions** (guidance for contributors):
  - `meta.description`: what the entity type represents and what distinguishes it — drives both LLM classification and prompt system message
  - Field `description`: what to extract and how — becomes per-field extraction instructions in the generated prompt
  - Composite type `description` (in `composite_types.json`): structural/behavioral instructions — injected automatically for any field using that type
  - `event_type.description`: always include "Choose the single most specific category that matches"
  - `date_range.description`: specify what dates the field refers to (e.g. "when the incident occurred")

- Document `{source_type}` context variable and its effect (social media date inference)
- Document env vars: `OPENROUTER_GENERATION_MODEL`, `OPENROUTER_FEEDBACK_MODEL`

---

## Execution Order

1. **Schema fixes** (Step 1) — foundation for everything
2. **`prompt_generator.py`** (Step 2) — core new file
3. **`extract.py` updates** (Step 3) — path fix + source_type
4. **Documentation** (Step 4)

## Files Modified

| File | Change |
|---|---|
| `src/schema/types/composite_types.json` | Add `DateFromUnstructured`, improve descriptions |
| `src/entities/extraction/schemas/*.json` (all 15) | Fix `event_type`/`theme_type`, `date_range` descriptions |
| `src/entities/extraction/schemas/public_works_event.json` | `completion_date` type → `DateFromUnstructured` |
| `src/entities/extraction/prompt_generator.py` | **New file** — PromptGenerationContextManager + PromptGeneration |
| `src/entities/extraction/extract.py` | Prompt path fix (line 313) + source_type context (line 561) |
| `src/entities/readme_entities.md` | Documentation update |
| `README.md` | Documentation update |

## Verification

1. Load each schema via `load_schema()` — verify `DateFromUnstructured` resolves correctly
2. Run `PromptGenerationContextManager("robbery_assault_event").to_dict()` — verify all fields and composite types gathered
3. Run `PromptGeneration().generate("emergency_event")` — verify output has correct format, all fields, template vars
4. Verify `_load_prompt("robbery_assault_event", {"date_now": "09/04/2026", "body": "test", "source_type": "news"})` works with new path
