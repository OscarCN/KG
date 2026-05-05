"""
Run the entity extraction pipeline on test data files.

Designed to be run step-by-step in IPython:
    ipython src/PoC/run_extraction.py

Or interactively in a Jupyter/IPython session using %run:
    %run src/PoC/run_extraction.py

The script is parameterized by the `DATA_SUBDIR` constant below — it reads
every `*.json` file under `data/<DATA_SUBDIR>/` and feeds the records to
the extractor. Two record shapes are supported automatically:

1. Facebook-style posts with a nested `message` dict (e.g. the
   `queretaro_fb_pages/` dataset).
2. News-style documents with flat fields `text`, `title`, `url`, etc.
   (e.g. the `legislative_gto/` dataset produced by `get_data.py`).
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from dotenv import load_dotenv

load_dotenv('../.env.local')

# Ensure project root is on sys.path
_PROJECT_ROOT = Path('/Users/oscarcuellar/ocn/media/kg/kg/')
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from src.entities.extraction.extract import EntityExtractor, Ontology

# ── Configuration ─────────────────────────────────────────────────────────────

# Which subdirectory under `data/` to read. Every `*.json` file in this
# directory is processed. Examples: "queretaro_fb_pages", "legislative_gto", "ayuntamiento_qro".
DATA_SUBDIR: str = "ayuntamiento_qro"

# Set to a list of Path objects to process specific files, or None to use all
# files found in data/<DATA_SUBDIR>/
FILES: list[Path] | None = None

# Set to True to only run keyword matching — no LLM calls
MATCH_ONLY: bool = False

# Max records to process per file. Set to None to process all
LIMIT: int | None = 500

# ── Load data ─────────────────────────────────────────────────────────────────

data_dir = _PROJECT_ROOT / "data" / DATA_SUBDIR

if FILES:
    files = FILES
else:
    files = sorted(data_dir.glob("*.json"))

print(f"Data dir: {data_dir}")
print(f"Files found: {len(files)}")
for f in files:
    print(f"  {f.name}")

# ── Helper ────────────────────────────────────────────────────────────────────

def _record_to_article(record: dict) -> dict:
    """Map a dataset record to the article dict expected by EntityExtractor.

    Supports two shapes:
    - Facebook post: `{"type": "Facebook", "message": {"body": ..., "title": ..., "url": ...}}`
    - News doc (ES hit): flat `{"text": ..., "title": ..., "url": ..., "custom_categories": {...}}`
    """
    msg = record.get("message")
    if isinstance(msg, dict):
        body = msg.get("body", "") or ""
        title = msg.get("title", "") or ""
        url = msg.get("url", "") or ""
        doc_type = (record.get("type") or msg.get("type") or "").lower()
        publication_date = msg.get("timestamp") or msg.get("created_time")

        categories: list[str] = []
        cat = msg.get("source_category")
        if cat:
            categories.append(cat) if isinstance(cat, str) else categories.extend(cat)
        tags = msg.get("source_tags")
        if tags:
            categories.extend(tags) if isinstance(tags, list) else categories.append(tags)
    else:
        body = record.get("text") or record.get("summary") or ""
        title = record.get("title") or ""
        url = record.get("url") or record.get("_id") or ""
        doc_type = (record.get("doctype") or record.get("type") or "news")
        if not isinstance(doc_type, str):
            doc_type = str(doc_type)
        doc_type = doc_type.lower()
        publication_date = (
            record.get("article_date")
            or record.get("date_created")
            or record.get("date")
            or record.get("published_at")
        )

        categories = []
        custom = record.get("custom_categories") or {}
        if isinstance(custom, dict):
            for level_vals in custom.values():
                if isinstance(level_vals, list):
                    categories.extend(level_vals)
                elif isinstance(level_vals, str):
                    categories.append(level_vals)

    return {
        "text": body,
        "title": title,
        "url": url,
        "categories": categories,
        "document_type": doc_type,
        "publication_date": publication_date,
    }

# ── Init extractor ────────────────────────────────────────────────────────────

ontology = Ontology()
extractor = EntityExtractor(ontology=ontology)

# ── Step 1: Keyword matching ───────────────────────────────────────────────────

matched_articles: list[tuple[dict, set]] = []   # (article, matched_classes)
all_records: list[dict] = []
for filepath in files:
    print(f"\n{'='*70}")
    print(f"File: {filepath.name}")
    print(f"{'='*70}")

    with open(filepath, encoding="utf-8") as f:
        records = json.load(f)

    if not isinstance(records, list):
        records = [records]

    all_records.extend(records)
    for i, record in enumerate(records):
        if LIMIT and i >= LIMIT:
            break

        article = _record_to_article(record)
        matched_classes = extractor.match(article)
        if not matched_classes:
            continue

        supertypes = ontology.resolve_supertypes(matched_classes)
        text_preview = (article["title"] or article["text"])[:80].replace("\n", " ")
        print(f"\n  Record {i+1}: {text_preview}...")
        print(f"  URL: {article['url']}")
        print(f"  Matched classes : {sorted(matched_classes)}")
        print(f"  Supertypes      : {sorted(supertypes)}")

        matched_articles.append((article, matched_classes))

print(f"\nTotal articles with keyword matches: {len(matched_articles)}")

# ── Step 2: LLM classification + extraction ───────────────────────────────────

if MATCH_ONLY:
    print("MATCH_ONLY=True — skipping LLM steps.")
else:
    all_entities: list[dict] = []

    for article, matched_classes in matched_articles:
        text_preview = (article["title"] or article["text"])[:60].replace("\n", " ")
        print(f"\n--- {text_preview}...", article.get("url"))

        try:
            entities = extractor.extract(
                article, validate=True, raise_validation_error=False,
            )
        except Exception as e:
            print(f"  ERROR: {e}")
            raise e
            #continue

        if not entities:
            print("  No entities extracted (LLM filtered all classes)")
            continue

        all_entities.extend(entities)
        for ent in entities:
            print(f"  -> [{ent.get('_supertype')}] {ent.get('event_type', ent.get('entity_type', ent.get('theme_type', '?')))}: "
                  f"{ent.get('name') or str(ent.get('description', ''))[:60]}")

    print(f"\n{'='*70}")
    print(f"Summary: {len(matched_articles)} matched, {len(all_entities)} entities extracted")

# ── Inspect results ────────────────────────────────────────────────────────────

# After running, `all_entities` holds all validated entity dicts.
# Example inspection:
#
#   all_entities[0]
#   [e for e in all_entities if e.get("_supertype") == "robbery_assault_event"]
