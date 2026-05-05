"""
Prompt generation system for entity extraction.

Auto-generates Spanish-language extraction prompts from JSON schemas using
a two-step LLM process: generation + feedback/revision.

Usage:
    from src.entities.extraction.prompt_generator import PromptGeneration

    gen = PromptGeneration()
    gen.generate("emergency")     # generate one supertype
    gen.generate_all()            # generate all supertypes
"""

from __future__ import annotations

import json
import logging
import os
import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Set

from src.llm.openrouter import call_openrouter

logger = logging.getLogger(__name__)

_BASE_DIR = Path(__file__).parent
_SCHEMAS_DIR = _BASE_DIR / "schemas"
_PROMPTS_DIR = _BASE_DIR / "prompts"
_PROMPTS_CLASSES_DIR = _PROMPTS_DIR / "classes"
_COMPOSITE_TYPES_PATH = Path(__file__).resolve().parent.parent.parent / "schema" / "types" / "composite_types.json"

# Reference prompt used as style exemplar for generation
_REFERENCE_PROMPT_PATH = _PROMPTS_CLASSES_DIR / "paid_mass_event.txt"


def _snake_to_pascal(name: str) -> str:
    """Convert snake_case supertype name to PascalCase schema key."""
    return "".join(word.capitalize() for word in name.split("_"))


def _get_available_supertypes() -> List[str]:
    """Discover available supertypes from schema JSON files in the schemas directory."""
    return sorted(p.stem for p in _SCHEMAS_DIR.glob("*.json"))


# ---------------------------------------------------------------------------
# Composite types loader (raw JSON, not resolved Python types)
# ---------------------------------------------------------------------------

def _load_raw_composite_types() -> Dict[str, Any]:
    """Load composite_types.json as raw JSON dict."""
    with open(_COMPOSITE_TYPES_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


# ---------------------------------------------------------------------------
# PromptGenerationContextManager
# ---------------------------------------------------------------------------

class PromptGenerationContextManager:
    """Gathers schema context for a supertype into a structured dict
    suitable for the prompt generation LLM.

    Three layers of context are assembled:
    1. Class-level: meta.description and meta.example
    2. Field-level: each field's type, description, required, enum
    3. Composite type-level: for fields referencing composite types,
       the type's meta.description and per-field descriptions
    """

    def __init__(self, supertype: str):
        schema_path = _SCHEMAS_DIR / f"{supertype}.json"
        if not schema_path.exists():
            available = _get_available_supertypes()
            raise ValueError(
                f"No schema file for supertype '{supertype}' at {schema_path}. "
                f"Available supertypes: {available}"
            )
        self.supertype = supertype
        self.schema_key = _snake_to_pascal(supertype)
        self._raw_schema = self._load_raw_schema()
        self._raw_composites = _load_raw_composite_types()
        self._context = self._build_context()

    def _load_raw_schema(self) -> dict:
        """Load the raw JSON schema file (type names stay as strings)."""
        path = _SCHEMAS_DIR / f"{self.supertype}.json"
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)

    def _build_context(self) -> dict:
        """Build the full context dictionary."""
        type_def = self._raw_schema[self.schema_key]
        schema_fields = type_def["schema"]

        return {
            "supertype": self.supertype,
            "schema_key": self.schema_key,
            "meta_category": type_def["meta"].get("category", "event"),
            "meta_description": type_def["meta"]["description"],
            "meta_example": type_def["meta"].get("example"),
            "fields": self._gather_fields(schema_fields),
            "composite_types": self._gather_composite_types(schema_fields),
            "raw_schema_json": json.dumps(self._raw_schema, indent=2, ensure_ascii=False),
        }

    def _gather_fields(self, schema: dict) -> List[dict]:
        """Return list of field dicts preserving schema order."""
        fields = []
        for name, spec in schema.items():
            fields.append({
                "name": name,
                "type": spec.get("type"),
                "description": spec.get("description"),
                "required": spec.get("required", False),
                "enum": spec.get("enum"),
            })
        return fields

    def _gather_composite_types(self, schema: dict) -> Dict[str, dict]:
        """For each field referencing a composite type, include the type's
        meta.description and field definitions. Recurse for transitive deps."""
        composites = {}
        to_resolve: Set[str] = set()

        # Collect type names referenced by fields
        for spec in schema.values():
            type_name = spec.get("type", "")
            inner = _extract_list_inner_type(type_name)
            candidate = inner if inner else type_name
            if candidate in self._raw_composites:
                to_resolve.add(candidate)

        # Resolve transitively
        resolved: Set[str] = set()
        while to_resolve:
            current = to_resolve.pop()
            if current in resolved:
                continue
            resolved.add(current)

            comp_def = self._raw_composites[current]
            comp_fields = []
            for fname, fspec in comp_def["schema"].items():
                comp_fields.append({
                    "name": fname,
                    "type": fspec.get("type"),
                    "description": fspec.get("description"),
                })
                # Check for transitive composite type references
                inner = _extract_list_inner_type(fspec.get("type", ""))
                candidate = inner if inner else fspec.get("type", "")
                if candidate in self._raw_composites and candidate not in resolved:
                    to_resolve.add(candidate)

            composites[current] = {
                "meta_description": comp_def.get("meta", {}).get("description", ""),
                "fields": comp_fields,
            }

        return composites

    def to_dict(self) -> dict:
        """Return the assembled context dictionary."""
        return self._context

    def to_json(self) -> str:
        """Return the assembled context as a JSON string."""
        return json.dumps(self._context, indent=2, ensure_ascii=False)


def _extract_list_inner_type(type_str: str) -> Optional[str]:
    """Extract the inner type from 'List[X]' patterns. Returns None if not a list type."""
    if not isinstance(type_str, str):
        return None
    match = re.match(r"^List\[(\w+)\]$", type_str)
    return match.group(1) if match else None


# ---------------------------------------------------------------------------
# Generation / feedback prompt templates
# ---------------------------------------------------------------------------

_GENERATION_SYSTEM = """\
You are an expert at writing structured extraction prompts for LLMs.
You will receive a schema definition for an entity type and a reference
example of a well-written extraction prompt. Your task is to generate
a new extraction prompt in Spanish following the same style and conventions."""

_GENERATION_USER_TEMPLATE = """\
Generate a Spanish-language extraction prompt for the entity type described below.
The prompt must follow the exact SYSTEM:/USER:/USER: format shown in the reference example.

## Reference Prompt (paid_mass_event — use this as your style guide)

{reference_prompt}

## Schema Context

{schema_context_json}

## Instructions

The schema belongs to one of three ontology categories, given in `meta_category`:

- **event**: identifiable single occurrences with a location and date (e.g. a concert, an arrest). \
Use item terminology like "evento" / "incidente" and require a date_range field.
- **theme**: broad topical classifiers (e.g. security, mobility). Use terminology like "tema" / \
"discusión". Date scope is optional and describes the temporal scope of the discourse, not a \
specific event date.
- **entity**: specific, identifiable things that are not events (e.g. a legislative initiative, a \
real estate development, a person). Use terminology like "entidad" / "iniciativa" / "ítem" as \
appropriate for the domain. Date fields are optional and describe attributes of the entity (e.g. \
date introduced, date founded), not a specific event datetime.

Adapt the wording, framing, and the "don't invent X" rule below to the category. For entities, \
instruct the LLM to only select items that are explicitly identifiable in the text (with name or \
distinguishing attributes), not generic mentions of the domain.

Generate a prompt with these sections:

### SYSTEM section
- Start with: "Eres un modelo para extraer información estructurada de [domain in Spanish] en artículos de noticias." \
The [domain in Spanish] should reflect the category — e.g. "eventos masivos" (event), \
"temas de seguridad" (theme), "iniciativas legislativas" (entity).
- Add a brief note explaining that the context variable {{source_type}} indicates the source type \
(e.g. "noticia", "publicación de Facebook", "publicación de red social"). \
For social media posts, dates can often be inferred from the publication date ({{date_now}}) \
if not explicitly mentioned in the text.

### First USER section
- Open with a Spanish paragraph describing what to extract, derived from the schema's meta_description. \
Translate naturally — do not just transliterate.
- Global rules: "Para cada [item name matching the category — evento / incidente / tema / iniciativa / \
entidad] mencionado en la nota, extrae los siguientes campos. Si no se menciona un campo, deja su \
valor null. No inventes [plural item name], solo extrae lo que esté explícitamente relacionado en \
la nota." For entity categories, add: "Selecciona solo ítems claramente identificables en el texto \
(con nombre propio o atributos distintivos), no por menciones generales del dominio."
- Number each field sequentially. For each field:
  - Use the format: "N. Spanish field name (json_key): instruction"
  - For EnumStr fields: render as a catalogue with format: "value" — Spanish label: brief description. \
The field instruction should say to choose the single most specific category that matches.
  - For str fields: translate the description into an instructional sentence in Spanish
  - For bool fields: explain true/false/null semantics
  - For List[str] fields: show an example array
  - For composite types (DateRangeFromUnstructured, DateFromUnstructured, Location, PriceRange, \
Attendance, VenueCapacity, CasualtyCount, CountMention, PersonReference, etc.):
    * Expand the type's structure with a JSON example that includes EVERY field defined in \
the composite type schema. No field may be omitted — show null for fields not applicable \
in the example. The JSON example must be an exact structural match to the composite type definition.
    * For fields with a "mention" subfield: instruct "Responde con el texto tal cual se menciona en la nota (mention)"
    * For DateRangeFromUnstructured / DateFromUnstructured: include detailed instructions about \
approximate dates and precision_days (e.g. "si solo se menciona un periodo aproximado como \
'la semana pasada', estima una fecha de inicio y un rango de precision en dias"), \
include 2-3 date examples with different precision levels, \
specify the date format "YYYY-MM-DDTHH:MM:SS", \
and inject the context: "Como contexto, la fecha de hoy es {{date_now}}"
    * For Location: the JSON example MUST include all 8 fields (country, state, city, \
neighborhood, zone, street, number, place_name) — use null for fields not present in the example. \
Instruct "Llena solo los campos mencionados explícitamente en la nota", \
and add EXPLICIT disambiguation between neighborhood / zone / place_name (these three fields are \
mutually exclusive and must never duplicate a name). The disambiguation block must include: \
(a) `neighborhood` is for ANY named residential area — colonias, fraccionamientos, barrios, \
unidades habitacionales, or named residential districts (give examples like "Centro", \
"Centro Histórico", "El Campanario", "Polanco", "Jurica", "Roma Norte"); \
(b) `zone` is ONLY for generic directional or functional zones without a residential proper name \
(give examples like "zona norte", "zona sur", "zona industrial", "zona metropolitana", \
"distrito financiero"). Named residential districts go in `neighborhood`, NOT in `zone`; \
(c) `place_name` is ONLY for a single point-like landmark — a venue, monument, plaza, park, \
station, named building, named intersection, etc. Never put a colonia, fraccionamiento or \
named residential district in `place_name`. Include 2–3 contrast examples like \
"'El Campanario' → neighborhood (no place_name, no zone)", \
"'Centro Histórico' → neighborhood (no place_name, no zone)", \
"'zona industrial' → zone", "'Estadio Corregidora' → place_name". Also include 2–3 generic \
INCORRECT examples ("frente a la escuela", "cerca del mercado", "varias calles del centro") \
that should be left null.
  - CRITICAL: every JSON example for a composite type must include ALL fields from that type's \
schema definition (in the composite_types section of the schema context). Fields not applicable \
in the example must be shown as null. Never abbreviate or truncate composite type examples — \
omitting fields causes the LLM to ignore those fields during extraction.
  - Use the field's description AND the composite type's meta_description to craft richer, \
more specific instructions. The three context layers (class description, field description, \
composite type description) should all inform the generated instruction.
- End with "Formato de respuesta:" section:
  - "Responde con una lista en formato JSON, donde cada elemento representa un [item name matching \
the category — evento / incidente / tema / iniciativa / entidad] detectado en la nota. No añadas \
texto adicional fuera del JSON."
  - Include the complete meta_example from the schema as the example, properly formatted as JSON

### Last USER section
- Just: "La noticia es la siguiente:\\n\\n{{body}}"

## Critical requirements
- Template variables {{date_now}}, {{source_type}}, and {{body}} MUST appear as literal \
placeholders with single curly braces (they are substituted at runtime)
- All text must be in Spanish (instructions, descriptions, labels)
- JSON keys in examples must be in English (matching the schema field names exactly)
- Every field in the schema must be covered — do not skip any
- Every enum value must appear in the catalogue
- Output ONLY the prompt text with SYSTEM:, USER:, USER: headers. No explanation or commentary."""

_FEEDBACK_SYSTEM = """\
You are a quality reviewer for LLM extraction prompts. Review the generated prompt \
against its source schema and the reference example. Identify issues and suggest improvements."""

_FEEDBACK_USER_TEMPLATE = """\
Review the following generated extraction prompt. Check it against the schema and the reference example.

## Generated Prompt

{draft}

## Schema Context

{schema_context_json}

## Reference Prompt (paid_mass_event)

{reference_prompt}

## Review Checklist

1. **Completeness**: Is every field from the schema covered in the prompt? List any missing fields.
2. **Consistency**: Do field names, types, and enum values match the schema exactly? List mismatches.
3. **Spanish quality**: Are instructions natural and instructional (not awkward translations)?
4. **Format**: Does it follow the SYSTEM:/USER:/USER: pattern correctly?
5. **Template variables**: Are {{date_now}}, {{source_type}}, and {{body}} present with single curly braces?
6. **Example JSON**: Does the example match the schema's meta_example structure?
7. **Composite type field completeness (CRITICAL)**: For EACH composite type used in the prompt \
(Location, DateRangeFromUnstructured, DateFromUnstructured, PriceRange, Attendance, VenueCapacity, \
CasualtyCount, CountMention, PersonReference, etc.), verify that the JSON example in the prompt \
includes EVERY field defined in that type's schema (listed in the composite_types section of the \
schema context). No field may be omitted — absent values must be shown as null. Specifically check:
   - Location MUST have all 8 fields: country, state, city, neighborhood, zone, street, number, place_name
   - DateRangeFromUnstructured MUST have all 4 fields: date_range (with start/end), timezone, mention, precision_days
   - DateFromUnstructured MUST have all 3 fields: date, mention, precision_days
   - CasualtyCount MUST have all 4 fields: mention, dead, injured, missing
   - CountMention MUST have all 3 fields: mention, count, confidence_range
   - PersonReference MUST have all 3 fields: name, role, organization
   List EVERY missing field as a separate issue.
8. **Date handling**: If the schema has a DateRangeFromUnstructured or DateFromUnstructured field, \
are approximate date instructions and precision_days examples included? (Skip this check if the \
schema has no date fields, which is common for entity/concept schemas.)
9. **Field name accuracy**: Do the field names in the JSON examples match the composite type \
schema exactly? E.g. CountMention uses "count" (not "estimate"), Attendance uses "estimate" \
(not "count"). Report any field name mismatches.

Respond with a numbered list of specific issues to fix. If no issues found, respond with "NO ISSUES FOUND"."""

_REVISION_USER_TEMPLATE = """\
Revise the following extraction prompt based on the feedback below. \
Fix all listed issues and return the corrected prompt.

## Current Prompt

{draft}

## Feedback

{feedback}

## Schema Context (for reference)

{schema_context_json}

Output ONLY the corrected prompt text with SYSTEM:, USER:, USER: headers. No explanation or commentary."""


# ---------------------------------------------------------------------------
# PromptGeneration
# ---------------------------------------------------------------------------

class PromptGeneration:
    """Generates Spanish extraction prompts from schema context using LLMs.

    Two-step process:
    1. Generate a draft prompt from schema context + reference example
    2. Send draft to a feedback LLM for review
    3. Apply feedback to produce the final prompt

    Environment variables:
        OPENROUTER_GENERATION_MODEL — model for generation (default: anthropic/claude-sonnet-4-20250514)
        OPENROUTER_FEEDBACK_MODEL   — model for feedback (default: anthropic/claude-sonnet-4-20250514)
    """

    GENERATION_MODEL_ENV = "OPENROUTER_GENERATION_MODEL"
    FEEDBACK_MODEL_ENV = "OPENROUTER_FEEDBACK_MODEL"
    DEFAULT_MODEL = "anthropic/claude-opus-4.6"

    def __init__(self):
        self.generation_model = os.environ.get(
            self.GENERATION_MODEL_ENV, self.DEFAULT_MODEL
        )
        self.feedback_model = os.environ.get(
            self.FEEDBACK_MODEL_ENV, self.DEFAULT_MODEL
        )
        self._reference_prompt = self._load_reference_prompt()

    @staticmethod
    def _load_reference_prompt() -> str:
        """Load the reference prompt (paid_mass_event.txt) as style exemplar."""
        if not _REFERENCE_PROMPT_PATH.exists():
            raise FileNotFoundError(
                f"Reference prompt not found at {_REFERENCE_PROMPT_PATH}. "
                f"Cannot generate prompts without a reference example."
            )
        return _REFERENCE_PROMPT_PATH.read_text(encoding="utf-8")

    def generate(self, supertype: str) -> str:
        """Full pipeline: gather context -> draft -> feedback -> revision -> save.

        Args:
            supertype: one of the 8 supertype names.

        Returns:
            The final prompt text.
        """
        logger.info("Generating prompt for supertype: %s", supertype)

        ctx_mgr = PromptGenerationContextManager(supertype)
        context = ctx_mgr.to_dict()
        context_json = ctx_mgr.to_json()

        # Step 1: Generate draft
        draft = self._generate_draft(context_json)
        logger.info("Draft generated for %s (%d chars)", supertype, len(draft))

        # Step 2: Get feedback
        feedback = self._get_feedback(draft, context_json)
        logger.info("Feedback received for %s", supertype)

        # Step 3: Apply feedback (skip if no issues)
        if "NO ISSUES FOUND" in feedback.upper():
            final = draft
            logger.info("No issues found, using draft as final for %s", supertype)
        else:
            final = self._apply_feedback(draft, feedback, context_json)
            logger.info("Feedback applied for %s (%d chars)", supertype, len(final))

        # Validate
        warnings = self._validate_prompt(final)
        for warning in warnings:
            logger.warning("Prompt validation [%s]: %s", supertype, warning)

        # Save
        self._save_prompt(supertype, final)
        logger.info("Prompt saved for %s", supertype)

        return final

    def _generate_draft(self, context_json: str) -> str:
        """Send the generation template + context to the generation LLM."""
        user_content = _GENERATION_USER_TEMPLATE.format(
            reference_prompt=self._reference_prompt,
            schema_context_json=context_json,
        )

        messages = [
            {"role": "system", "content": _GENERATION_SYSTEM},
            {"role": "user", "content": user_content},
        ]

        logger.debug(
            "Generation prompt (system): %s", _GENERATION_SYSTEM[:200]
        )
        logger.debug(
            "Generation prompt (user): %d chars", len(user_content)
        )
        logger.debug("Generation prompt (user) full:\n%s", user_content)

        result = call_openrouter(
            messages,
            model=self.generation_model,
            max_tokens=8192,
            temperature=0.3,
        )

        logger.debug("Draft output:\n%s", result)
        return result

    def _get_feedback(self, draft: str, context_json: str) -> str:
        """Send draft + schema to feedback LLM for review."""
        user_content = _FEEDBACK_USER_TEMPLATE.format(
            draft=draft,
            schema_context_json=context_json,
            reference_prompt=self._reference_prompt,
        )

        messages = [
            {"role": "system", "content": _FEEDBACK_SYSTEM},
            {"role": "user", "content": user_content},
        ]

        logger.debug(
            "Feedback prompt (user): %d chars", len(user_content)
        )
        logger.debug("Feedback prompt (user) full:\n%s", user_content)

        result = call_openrouter(
            messages,
            model=self.feedback_model,
            max_tokens=4096,
            temperature=0.2,
        )

        logger.debug("Feedback output:\n%s", result)
        return result

    def _apply_feedback(self, draft: str, feedback: str, context_json: str) -> str:
        """Send draft + feedback back to generation LLM for revision."""
        user_content = _REVISION_USER_TEMPLATE.format(
            draft=draft,
            feedback=feedback,
            schema_context_json=context_json,
        )

        messages = [
            {"role": "system", "content": _GENERATION_SYSTEM},
            {"role": "user", "content": user_content},
        ]

        logger.debug(
            "Revision prompt (user): %d chars", len(user_content)
        )
        logger.debug("Revision prompt (user) full:\n%s", user_content)

        result = call_openrouter(
            messages,
            model=self.generation_model,
            max_tokens=8192,
            temperature=0.2,
        )

        logger.debug("Revision output:\n%s", result)
        return result

    @staticmethod
    def _validate_prompt(prompt_text: str) -> List[str]:
        """Basic sanity checks on the generated prompt. Returns warnings."""
        warnings = []

        if "SYSTEM:" not in prompt_text:
            warnings.append("Missing SYSTEM: header")
        if "USER:" not in prompt_text:
            warnings.append("Missing USER: header")
        if "{body}" not in prompt_text:
            warnings.append("Missing {body} template variable")
        if "{date_now}" not in prompt_text:
            warnings.append("Missing {date_now} template variable")
        if "{source_type}" not in prompt_text:
            warnings.append("Missing {source_type} template variable")

        return warnings

    def _save_prompt(self, supertype: str, prompt_text: str) -> None:
        """Save the generated prompt to prompts/classes/{supertype}.txt."""
        _PROMPTS_CLASSES_DIR.mkdir(parents=True, exist_ok=True)
        path = _PROMPTS_CLASSES_DIR / f"{supertype}.txt"
        path.write_text(prompt_text, encoding="utf-8")

    def generate_all(self) -> Dict[str, str]:
        """Generate prompts for all supertypes.

        Returns:
            Dict mapping supertype name to generated prompt text.
        """
        results = {}
        for supertype in _get_available_supertypes():
            results[supertype] = self.generate(supertype)
        return results
