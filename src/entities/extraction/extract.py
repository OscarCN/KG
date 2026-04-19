"""
Entity extraction pipeline.

Receives articles, matches keywords against the ontology, selects the
corresponding schemas and prompts, sends to an LLM for extraction, and
parses/validates the results through the schema system.

Usage:
    extractor = EntityExtractor()
    results = extractor.extract(article)
"""

from __future__ import annotations

import csv
import hashlib
import json
import logging
import re
import unicodedata
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

import pandas as pd
from nltk.stem.snowball import SpanishStemmer

from src.schema.schemas.read_schema import load_schema
from src.schema.parse_object import Parser
from src.llm.openrouter import call_openrouter

logger = logging.getLogger(__name__)


_BASE_DIR = Path(__file__).parent
_CATALOGUES_DIR = _BASE_DIR / "catalogues"
_SCHEMAS_DIR = _BASE_DIR / "schemas"
_PROMPTS_DIR = _BASE_DIR / "prompts"
_CACHE_DIR = Path(__file__).resolve().parents[3] / "cache"


def _snake_to_pascal(name: str) -> str:
    """Convert snake_case supertype name to PascalCase schema key."""
    return "".join(word.capitalize() for word in name.split("_"))


# ---------------------------------------------------------------------------
# Text normalization helpers
# ---------------------------------------------------------------------------

_stemmer = SpanishStemmer()


def _normalize_text(text: str) -> str:
    """Lowercase, strip accents, collapse whitespace."""
    text = text.lower()
    text = unicodedata.normalize("NFD", text)
    text = "".join(c for c in text if unicodedata.category(c) != "Mn")
    text = re.sub(r"\s+", " ", text).strip()
    return text


def _stem_text(text: str) -> str:
    """Stem each word in a normalized text string."""
    return " ".join(_stemmer.stem(w) for w in text.split())


# ---------------------------------------------------------------------------
# Ontology — loads catalogues and performs keyword matching
# ---------------------------------------------------------------------------

class Ontology:
    """Loads the ontology catalogues and resolves keywords → supertypes.

    Matching rules are loaded from an Excel file (keywords.xlsx). Each row
    is an independent matching rule. Within a row, all non-empty columns
    are AND'd together. Across rows, matches are OR'd.

    Columns used for matching:
        - class: ontology class (event_type) assigned when the row matches
        - kw: quoted comma-separated keywords (OR) — matched with stemming
        - phrase: quoted comma-separated phrases (OR) — matched exactly (no stemming)
        - not: quoted comma-separated keywords (OR) — text must NOT contain any
        - categories: pipe-separated categories (OR) — doc must have any
        - dismiss_categories: pipe-separated categories (OR) — doc must NOT have any
        - document_type: comma-separated types (OR) — doc type must match any
        - location, bbox, period: not used in matching (reserved)
    """

    def __init__(
        self,
        event_types_path: Path = _CATALOGUES_DIR / "event_types.csv",
        keywords_path: Path = _CATALOGUES_DIR / "keywords.xlsx",
    ):
        # event_type → supertype
        self.type_to_supertype: Dict[str, str] = {}
        # event_type → {label_es, label_en}
        self.type_labels: Dict[str, Dict[str, str]] = {}

        with open(event_types_path, newline="", encoding="utf-8") as f:
            for row in csv.DictReader(f):
                et = row["event_type"]
                self.type_to_supertype[et] = row["supertype"]
                self.type_labels[et] = {
                    "label_es": row["label_es"],
                    "label_en": row["label_en"],
                }

        # Load matching rules from Excel
        df = pd.read_excel(keywords_path)
        self.rules: List[Dict[str, Any]] = []
        for _, row in df.iterrows():
            rule: Dict[str, Any] = {}
            rule["ontology_class"] = str(row["class"]).strip() if pd.notna(row.get("class")) else None
            rule["kw"] = _parse_quoted_list(row.get("kw"))
            rule["kw_stemmed"] = [_stem_text(kw) for kw in rule["kw"]]
            rule["phrase"] = _parse_quoted_list(row.get("phrase"))
            rule["not"] = _parse_quoted_list(row.get("not"))
            rule["categories"] = _parse_pipe_list(row.get("categories"))
            rule["dismiss_categories"] = _parse_pipe_list(row.get("dismiss_categories"))
            rule["document_type"] = _parse_comma_list(row.get("document_type"))
            if rule["ontology_class"]:
                self.rules.append(rule)

    def match(
        self,
        text: str = "",
        categories: Optional[List[str]] = None,
        document_type: str = "",
    ) -> Set[str]:
        """Match an article against all rules. Returns matched ontology classes.

        Args:
            text: article text (title + body).
            categories: list of article categories from source metadata.
            document_type: document type string (e.g. "news", "facebook").

        Returns:
            Set of ontology class names (event_types) that matched.
        """
        norm_text = _normalize_text(text) if text else ""
        stemmed_words = set(_stem_text(norm_text).split()) if norm_text else set()
        norm_doc_type = document_type.lower().strip() if document_type else ""
        cats = set(categories) if categories else set()

        matched: Set[str] = set()
        for rule in self.rules:
            if self._rule_matches(rule, norm_text, stemmed_words, cats, norm_doc_type):
                matched.add(rule["ontology_class"])
        return matched

    @staticmethod
    def _rule_matches(
        rule: Dict[str, Any],
        norm_text: str,
        stemmed_words: set,
        categories: set,
        document_type: str,
    ) -> bool:
        """Check if a single rule matches. All non-empty conditions must pass (AND).

        kw and phrase are OR'd together: if a row has both, matching either
        satisfies the text condition. If only one is present, that one must match.
        """
        # kw (stemmed, word-level) + phrase (exact substring): OR'd together
        has_kw = bool(rule["kw_stemmed"])
        has_phrase = bool(rule["phrase"])
        if has_kw or has_phrase:
            kw_hit = has_kw and any(
                _stemmed_kw_matches(kw, stemmed_words) for kw in rule["kw_stemmed"]
            )
            phrase_hit = has_phrase and any(p in norm_text for p in rule["phrase"])
            if not (kw_hit or phrase_hit):
                return False

        # not: text must NOT contain any keyword (exact, no stemming)
        if rule["not"]:
            if any(kw in norm_text for kw in rule["not"]):
                return False

        # categories: doc must have at least one listed category (OR)
        if rule["categories"]:
            if not categories.intersection(rule["categories"]):
                return False

        # dismiss_categories: doc must NOT have any listed category
        if rule["dismiss_categories"]:
            if categories.intersection(rule["dismiss_categories"]):
                return False

        # document_type: doc type must match at least one (OR)
        if rule["document_type"]:
            if not any(document_type == dt for dt in rule["document_type"]):
                return False

        return True

    def match_text(self, text: str) -> Set[str]:
        """Return set of event_types matched by keywords found in text.

        Backward-compatible convenience method. For full matching with
        categories and document_type, use match() instead.
        """
        return self.match(text=text)

    def match_categories(self, categories: List[str]) -> Set[str]:
        """Return set of event_types matched by article categories.

        Backward-compatible convenience method. For full matching with
        all filters, use match() instead.
        """
        return self.match(categories=categories)

    def resolve_supertypes(self, event_types: Set[str]) -> Set[str]:
        """Map a set of event_types to their supertypes (deduped)."""
        return {
            self.type_to_supertype[et]
            for et in event_types
            if et in self.type_to_supertype
        }

    def get_class_descriptions(self, event_types: Set[str]) -> List[Dict[str, str]]:
        """Build a description list for a set of ontology classes.

        Returns one entry per class with its name, Spanish label, supertype,
        ontology category (event/theme/entity, from the schema's meta.category),
        and the description from the supertype's schema meta.description.
        Used to present matched classes to the LLM for classification.
        """
        descriptions = []
        for et in sorted(event_types):
            supertype = self.type_to_supertype.get(et)
            if not supertype:
                continue
            labels = self.type_labels.get(et, {})
            loaded = _get_schema(supertype)
            schema_key = _snake_to_pascal(supertype)
            meta = loaded.get("meta", {}).get(schema_key, {})
            descriptions.append({
                "class": et,
                "label_es": labels.get("label_es", et),
                "label_en": labels.get("label_en", et),
                "supertype": supertype,
                "category": meta.get("category", "event"),
                "description": meta.get("description", ""),
            })
        return descriptions


def _stemmed_kw_matches(stemmed_kw: str, stemmed_words: set) -> bool:
    """Check if a stemmed keyword (possibly multi-word) matches the stemmed word set.

    Single-word keywords check word membership. Multi-word stemmed keywords
    check that all words are present in the text.
    """
    kw_words = stemmed_kw.split()
    if len(kw_words) == 1:
        return kw_words[0] in stemmed_words
    return all(w in stemmed_words for w in kw_words)


def _parse_quoted_list(value) -> List[str]:
    """Parse a cell like '"word1","word2"' into a list of normalized strings."""
    if pd.isna(value) or not str(value).strip():
        return []
    raw = str(value)
    # Extract quoted strings, or fall back to comma split
    items = re.findall(r'"([^"]+)"', raw)
    if not items:
        items = [s.strip() for s in raw.split(",") if s.strip()]
    return [_normalize_text(item) for item in items]


def _parse_pipe_list(value) -> List[str]:
    """Parse a pipe-separated cell into a list of stripped strings."""
    if pd.isna(value) or not str(value).strip():
        return []
    return [s.strip() for s in str(value).split("|") if s.strip()]


def _parse_comma_list(value) -> List[str]:
    """Parse a comma-separated cell into a list of lowercase stripped strings."""
    if pd.isna(value) or not str(value).strip():
        return []
    return [s.strip().lower() for s in str(value).split(",") if s.strip()]


# ---------------------------------------------------------------------------
# Prompt loader — reads cached prompt files
# ---------------------------------------------------------------------------

def _load_prompt(supertype: str, context: Dict[str, str]) -> List[Dict[str, str]]:
    """Load a cached prompt file and return LLM messages.

    Prompt files use the format:
        SYSTEM:\n<text>\n\nUSER:\n<text>\n\nUSER:\n<text>

    Context variables like {date_now} and {body} are substituted.
    """
    path = _PROMPTS_DIR / "classes" / f"{supertype}.txt"
    if not path.exists():
        raise FileNotFoundError(
            f"No prompt file for supertype '{supertype}' at {path}. "
            f"Generate it first or create it manually."
        )

    raw = path.read_text(encoding="utf-8")

    # Split on section headers: "SYSTEM:" or "USER:"
    sections = re.split(r"^(SYSTEM|USER):\s*$", raw, flags=re.MULTILINE)

    messages: List[Dict[str, str]] = []
    i = 1  # skip leading empty split
    while i < len(sections):
        role_label = sections[i].strip().lower()
        content = sections[i + 1].strip() if i + 1 < len(sections) else ""
        role = "system" if role_label == "system" else "user"
        # Apply context substitutions
        for key, value in context.items():
            content = content.replace(f"{{{key}}}", value)
        messages.append({"role": role, "content": content})
        i += 2

    return messages


# ---------------------------------------------------------------------------
# Schema loader cache
# ---------------------------------------------------------------------------

_schema_cache: Dict[str, Dict[str, Any]] = {}


def _get_schema(supertype: str) -> Dict[str, Any]:
    """Load and cache a supertype's schema."""
    if supertype not in _schema_cache:
        path = _SCHEMAS_DIR / f"{supertype}.json"
        _schema_cache[supertype] = load_schema(path)
    return _schema_cache[supertype]


# ---------------------------------------------------------------------------
# Extraction cache — stores per-(article, class) results in cache/
# ---------------------------------------------------------------------------


def _cache_key(article_url: str, class_name: str) -> str:
    """Build a hex cache key from hash((article_url, class_name))."""
    h = hashlib.sha256(f"{article_url}|{class_name}".encode()).hexdigest()
    return h


def _cache_read(article_url: str, class_name: str) -> Optional[List[Dict[str, Any]]]:
    """Return cached extraction results, or None on miss."""
    path = _CACHE_DIR / f"{_cache_key(article_url, class_name)}.json"
    if not path.exists():
        return None
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return None


def _cache_write(
    article_url: str, class_name: str, entities: List[Dict[str, Any]]
) -> None:
    """Write extraction results to cache."""
    _CACHE_DIR.mkdir(parents=True, exist_ok=True)
    path = _CACHE_DIR / f"{_cache_key(article_url, class_name)}.json"
    with open(path, "w", encoding="utf-8") as f:
        json.dump(entities, f, ensure_ascii=False, default=str)


def _classify_cache_key(article_url: str, matched_classes: Set[str]) -> str:
    """Build a hex cache key for classification from the article URL and candidate classes."""
    classes_str = ",".join(sorted(matched_classes))
    h = hashlib.sha256(f"classify|{article_url}|{classes_str}".encode()).hexdigest()
    return h


def _classify_cache_read(
    article_url: str, matched_classes: Set[str]
) -> Optional[List[str]]:
    """Return cached classification results, or None on miss."""
    path = _CACHE_DIR / f"{_classify_cache_key(article_url, matched_classes)}.json"
    if not path.exists():
        return None
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return None


def _classify_cache_write(
    article_url: str, matched_classes: Set[str], confirmed: List[str]
) -> None:
    """Write classification results to cache."""
    _CACHE_DIR.mkdir(parents=True, exist_ok=True)
    path = _CACHE_DIR / f"{_classify_cache_key(article_url, matched_classes)}.json"
    with open(path, "w", encoding="utf-8") as f:
        json.dump(confirmed, f, ensure_ascii=False)


# ---------------------------------------------------------------------------
# LLM call — scaffold
# ---------------------------------------------------------------------------

def call_llm(messages: List[Dict[str, str]]) -> str:
    """Send messages to an LLM and return the raw response text.

    Uses OpenRouter as the LLM provider. Requests JSON mode to ensure
    the response is valid JSON for downstream parsing.

    Requires OPENROUTER_API_KEY environment variable. Model can be
    overridden via OPENROUTER_MODEL (defaults to openai/gpt-4o).

    Args:
        messages: list of {"role": "system"|"user", "content": "..."} dicts

    Returns:
        The raw string content of the LLM response (expected to be JSON).
    """
    return call_openrouter(
        messages,
        response_format={"type": "json_object"},
    )


# ---------------------------------------------------------------------------
# Response parsing
# ---------------------------------------------------------------------------

def _parse_llm_response(raw: str) -> List[Dict[str, Any]]:
    """Parse the raw LLM response string into a list of entity dicts.

    Handles common LLM quirks: markdown code fences, trailing commas.
    """
    text = raw.strip()

    # Strip markdown code fences
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text)

    # Remove trailing commas before } or ] (common LLM mistake)
    text = re.sub(r",\s*([}\]])", r"\1", text)

    parsed = json.loads(text)

    if isinstance(parsed, dict):
        return [parsed]
    if isinstance(parsed, list):
        return parsed

    raise ValueError(f"Expected list or dict from LLM, got {type(parsed).__name__}")


def _validate_entity(
    entity: Dict[str, Any],
    supertype: str,
) -> Dict[str, Any]:
    """Run an entity dict through the schema parser for type coercion and validation.

    Returns the normalized entity. Raises on validation failure.
    """
    loaded = _get_schema(supertype)
    schema_key = _snake_to_pascal(supertype)
    parser = Parser(loaded["schemas"])
    return parser.normalize_record(entity, schema_key)


# ---------------------------------------------------------------------------
# EntityExtractor — main pipeline
# ---------------------------------------------------------------------------

class EntityExtractor:
    """Extracts structured entities from articles using the ontology + LLM.

    Pipeline:
        1. Keyword matching → candidate ontology classes
        2. LLM classification → confirmed classes the article actually refers to
        3. Per-class LLM extraction → structured entities

    Usage:
        extractor = EntityExtractor()
        results = extractor.extract({
            "text": "Dos sujetos armados asaltaron una tienda...",
            "title": "Asalto en León",
            "categories": ["Seguridad"],
        })
    """

    def __init__(self, ontology: Optional[Ontology] = None):
        self.ontology = ontology or Ontology()

    def match(self, article: Dict[str, Any]) -> Set[str]:
        """Match an article against the ontology. Returns matched ontology classes.

        Returns:
            Set of ontology class names (event_types) that matched keyword rules.
        """
        text = article.get("title", "") + " " + article.get("text", "")
        categories = article.get("categories", [])
        if not isinstance(categories, list):
            categories = []
        document_type = article.get("document_type", "")
        if not isinstance(document_type, str):
            document_type = str(document_type) if document_type else ""

        return self.ontology.match(
            text=text,
            categories=categories,
            document_type=document_type,
        )

    def classify(
        self,
        article: Dict[str, Any],
        matched_classes: Set[str],
    ) -> List[str]:
        """Ask the LLM which of the matched ontology classes the article actually refers to.

        Presents the LLM with the article text and the subset of ontology
        classes (with descriptions) that matched keyword rules. Classes may
        refer to identifiable events, entities, concepts, or themes. The LLM
        determines which classes the article genuinely discusses.

        Args:
            article: dict with at least "text", optionally "title".
            matched_classes: set of ontology class names from keyword matching.

        Returns:
            List of ontology class names the LLM confirmed as present.
        """
        class_descriptions = self.ontology.get_class_descriptions(matched_classes)
        if not class_descriptions:
            return []

        # Check classification cache
        article_url = article.get("url") or article.get("id") or ""
        if article_url:
            cached = _classify_cache_read(article_url, matched_classes)
            if cached is not None:
                logger.debug(
                    "Classify cache hit for (%s) — %d classes",
                    article_url, len(cached),
                )
                return cached

        # Separate classes by ontology category for the prompt. Each category
        # has different selection criteria. Routing is driven by each schema's
        # meta.category ("event" | "theme" | "entity").
        event_lines: List[str] = []
        theme_lines: List[str] = []
        entity_lines: List[str] = []
        for desc in class_descriptions:
            line = f'- "{desc["class"]}" — {desc["label_es"]}: {desc["description"]}'
            category = desc.get("category", "event")
            if category == "theme":
                theme_lines.append(line)
            elif category == "entity":
                entity_lines.append(line)
            else:
                event_lines.append(line)

        catalogue_parts = []
        if event_lines:
            catalogue_parts.append(
                "Eventos (ocurrencias específicas con ubicación y fecha — "
                "selecciona solo si el artículo reporta o describe un evento "
                "identificable de este tipo):\n\n" + "\n".join(event_lines)
            )
        if theme_lines:
            catalogue_parts.append(
                "Temas (clasificadores temáticos amplios — selecciona siempre "
                "que el artículo toque, mencione o discuta cualquier asunto "
                "relacionado con el tema, aunque sea de paso o como contexto):\n\n"
                + "\n".join(theme_lines)
            )
        if entity_lines:
            catalogue_parts.append(
                "Entidades/Conceptos (cosas específicas e identificables que no son "
                "eventos ni temas — selecciona solo si el artículo se refiere a una "
                "entidad o concepto concreto de este tipo, con nombre propio o "
                "atributos identificables; no selecciones por mención general del "
                "dominio ni por discusión temática):\n\n" + "\n".join(entity_lines)
            )
        catalogue_block = "\n\n".join(catalogue_parts)

        body = article.get("text", "")
        title = article.get("title", "")
        article_text = f"{title}\n\n{body}" if title else body

        messages = [
            {
                "role": "system",
                "content": (
                    "Eres un modelo de clasificación de artículos y publicaciones. "
                    "Se te presenta un artículo y un catálogo de categorías dividido "
                    "en hasta tres grupos: eventos, temas y entidades/conceptos.\n\n"
                    "EVENTOS: son ocurrencias específicas e identificables (con lugar "
                    "y fecha). Selecciona un evento solo si el artículo reporta o "
                    "describe un evento concreto de ese tipo.\n\n"
                    "TEMAS: son clasificadores temáticos amplios. Selecciona un tema "
                    "siempre que el artículo toque, mencione o discuta cualquier asunto "
                    "relacionado — ya sea como tema principal, secundario, o incluso "
                    "como contexto o mención de paso. Un artículo que reporta un robo "
                    "también toca el tema de seguridad.\n\n"
                    "ENTIDADES/CONCEPTOS: son cosas específicas e identificables que "
                    "no son eventos ni temas — por ejemplo una iniciativa de ley "
                    "concreta, un desarrollo inmobiliario, una persona específica, una "
                    "tecnología o un compuesto. Selecciona una entidad/concepto solo "
                    "si el artículo se refiere a un ítem concreto de ese tipo (con "
                    "nombre propio o atributos identificables), no por una mención "
                    "general del dominio ni por discusión temática.\n\n"
                    "Un artículo puede clasificarse en múltiples categorías de los "
                    "tres grupos simultáneamente."
                ),
            },
            {
                "role": "user",
                "content": (
                    f"Dado el siguiente artículo, indica cuáles de las categorías "
                    f"listadas abajo aplican.\n\n"
                    f"Catálogo de categorías candidatas:\n\n"
                    f"{catalogue_block}\n\n"
                    f"Artículo:\n\n{article_text}\n\n"
                    f"Responde con un JSON de la forma:\n"
                    f'{{"classes": ["class1", "class2"]}}\n\n'
                    f"Si ninguna categoría aplica, responde con una lista vacía: "
                    f'{{"classes": []}}'
                ),
            },
        ]

        raw = call_llm(messages)
        text = raw.strip()
        # Strip markdown code fences the LLM sometimes wraps around JSON
        if text.startswith("```"):
            text = re.sub(r"^```(?:json)?\s*", "", text)
            text = re.sub(r"\s*```$", "", text)
        parsed = json.loads(text)
        confirmed = parsed.get("classes", [])

        # Filter to only classes that were in the original matched set
        valid_classes = {d["class"] for d in class_descriptions}
        confirmed = [c for c in confirmed if c in valid_classes]

        # Write to classification cache
        if article_url:
            _classify_cache_write(article_url, matched_classes, confirmed)

        # Debug logging
        text_snippet = article_text[:200].replace("\n", " ")
        candidate_summary = [
            f'{d["class"]} ({d["supertype"]})'
            for d in class_descriptions
        ]
        logger.debug(
            "Classification — text: '%.200s...' | candidates: %s | confirmed: %s",
            text_snippet,
            candidate_summary,
            confirmed,
        )

        return confirmed

    def extract_supertype(
        self,
        article: Dict[str, Any],
        supertype: str,
        event_type: Optional[str] = None,
        validate: bool = True,
    ) -> List[Dict[str, Any]]:
        """Run extraction for a single supertype against an article.

        Args:
            article: dict with at least "text" key, optionally "title", "date".
            supertype: which supertype schema/prompt to use.
            event_type: if provided, instruct the LLM to extract only events
                of this specific ontology class. When None, extracts all types
                within the supertype (legacy behavior).
            validate: if True, run results through schema validation.

        Returns:
            List of extracted entity dicts.
        """
        date_now = datetime.now().strftime("%d/%m/%Y")
        body = article.get("text", "")
        source_type = article.get("source_type", "news")

        context = {"date_now": date_now, "body": body, "source_type": source_type}
        messages = _load_prompt(supertype, context)

        # Inject a focus instruction when extracting for a specific class
        if event_type:
            labels = self.ontology.type_labels.get(event_type, {})
            label_es = labels.get("label_es", event_type)
            focus_msg = {
                "role": "user",
                "content": (
                    f"IMPORTANTE: Para este artículo, extrae únicamente entradas "
                    f'de tipo "{event_type}" ({label_es}). '
                    f"Ignora cualquier otro tipo que pueda aparecer en la nota."
                ),
            }
            # Insert focus instruction before the last USER message (the article)
            messages.insert(-1, focus_msg)

        raw_response = call_llm(messages)

        entities = _parse_llm_response(raw_response)

        # Tag each entity with source metadata
        article_id = article.get("id") or article.get("url")
        for entity in entities:
            entity["_source_id"] = article_id
            entity["_supertype"] = supertype

        if validate:
            validated = []
            for entity in entities:
                meta = {
                    "_source_id": entity.pop("_source_id", None),
                    "_supertype": entity.pop("_supertype", None),
                }
                normalized = _validate_entity(entity, supertype)
                normalized.update(meta)
                validated.append(normalized)
            return validated

        return entities

    def extract(
        self,
        article: Dict[str, Any],
        validate: bool = True,
    ) -> List[Dict[str, Any]]:
        """Full extraction pipeline for an article.

        1. Match keywords/categories → candidate ontology classes
        2. LLM classification → confirmed classes the article actually refers to
        3. For each confirmed class, extract structured events using
           the class's supertype schema, scoped to that specific class

        Args:
            article: dict with "text" (required), and optionally
                     "title", "date", "categories", "id"/"url".
            validate: if True, run results through schema validation.

        Returns:
            List of extracted entity dicts across all confirmed classes.
        """
        matched_classes = self.match(article)

        if not matched_classes:
            return []

        # Step 2: LLM classification — which classes does the article actually refer to?
        confirmed_classes = self.classify(article, matched_classes)

        if not confirmed_classes:
            return []

        # Group confirmed classes by supertype. Multiple confirmed classes may
        # share a supertype (e.g. pedestrian_hit + emergency_general both map to
        # emergency_event). When that happens, extract once per supertype without
        # a class focus so the LLM extracts all relevant entries under that schema.
        supertype_to_classes: Dict[str, List[str]] = {}
        for et in confirmed_classes:
            st = self.ontology.type_to_supertype.get(et)
            if st:
                supertype_to_classes.setdefault(st, []).append(et)

        if len(supertype_to_classes) < len(confirmed_classes):
            logger.debug(
                "Grouped classes by supertype: %s → %s",
                confirmed_classes,
                {st: cls for st, cls in supertype_to_classes.items()},
            )

        # Step 3: Extract per supertype (with cache)
        article_url = article.get("url") or article.get("id") or ""
        all_entities: List[Dict[str, Any]] = []
        for supertype, classes in supertype_to_classes.items():
            # Single class → focused extraction; multiple → unfocused (all types)
            event_type = classes[0] if len(classes) == 1 else None
            cache_key_class = event_type or supertype

            # Check cache
            if article_url:
                cached = _cache_read(article_url, cache_key_class)
                if cached is not None:
                    logger.debug(
                        "Cache hit for (%s, %s) — %d entities",
                        article_url, cache_key_class, len(cached),
                    )
                    all_entities.extend(cached)
                    continue

            entities = self.extract_supertype(
                article, supertype, event_type=event_type, validate=validate,
            )

            # Write to cache
            if article_url:
                _cache_write(article_url, cache_key_class, entities)

            all_entities.extend(entities)

        return all_entities
