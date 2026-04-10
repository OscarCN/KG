"""
Run the entity extraction pipeline on test data files.

Designed to be run step-by-step in IPython:
    ipython src/PoC/run_extraction.py

Or interactively in a Jupyter/IPython session using %run:
    %run src/PoC/run_extraction.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

# Ensure project root is on sys.path
_PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from src.entities.extraction.extract import EntityExtractor, Ontology

# ── Configuration ─────────────────────────────────────────────────────────────

# Set to a list of Path objects to process specific files, or None to use all
# files found in data/queretaro_fb_pages/
FILES: list[Path] | None = None

# Set to True to only run keyword matching — no LLM calls
MATCH_ONLY: bool = False

# Max posts to process per file. Set to None to process all
LIMIT: int | None = 5

# ── Load data ─────────────────────────────────────────────────────────────────

data_dir = _PROJECT_ROOT / "data" / "queretaro_fb_pages"

if FILES:
    files = FILES
else:
    files = sorted(data_dir.glob("*.json"))

print(f"Files found: {len(files)}")
for f in files:
    print(f"  {f.name}")

# ── Helper ────────────────────────────────────────────────────────────────────

def _post_to_article(post: dict) -> dict:
    """Map a Facebook page post to the article dict expected by EntityExtractor."""
    msg = post.get("message", {})
    body = msg.get("body", "") or ""
    title = msg.get("title", "") or ""
    url = msg.get("url", "")
    doc_type = (post.get("type") or msg.get("type") or "").lower()

    categories = []
    cat = msg.get("source_category")
    if cat:
        categories.append(cat) if isinstance(cat, str) else categories.extend(cat)
    tags = msg.get("source_tags")
    if tags:
        categories.extend(tags) if isinstance(tags, list) else categories.append(tags)

    return {
        "text": body,
        "title": title,
        "url": url,
        "categories": categories,
        "document_type": doc_type,
    }

# ── Init extractor ────────────────────────────────────────────────────────────

ontology = Ontology()
extractor = EntityExtractor(ontology=ontology)

# ── Step 1: Keyword matching ───────────────────────────────────────────────────

matched_articles: list[tuple[dict, set]] = []   # (article, matched_classes)

for filepath in files:
    print(f"\n{'='*70}")
    print(f"File: {filepath.name}")
    print(f"{'='*70}")

    with open(filepath, encoding="utf-8") as f:
        posts = json.load(f)

    if not isinstance(posts, list):
        posts = [posts]

    for i, post in enumerate(posts):
        if LIMIT and i >= LIMIT:
            break

        article = _post_to_article(post)
        matched_classes = extractor.match(article)
        if not matched_classes:
            continue

        supertypes = ontology.resolve_supertypes(matched_classes)
        text_preview = (article["title"] or article["text"])[:80].replace("\n", " ")
        print(f"\n  Post {i+1}: {text_preview}...")
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
        print(f"\n--- {text_preview}...")

        try:
            entities = extractor.extract(article, validate=True)
        except Exception as e:
            print(f"  ERROR: {e}")
            continue

        if not entities:
            print("  No entities extracted (LLM filtered all classes)")
            continue

        all_entities.extend(entities)
        for ent in entities:
            print(f"  -> [{ent.get('_supertype')}] {ent.get('event_type', '?')}: "
                  f"{ent.get('name') or ent.get('description', '')[:60]}")

    print(f"\n{'='*70}")
    print(f"Summary: {len(matched_articles)} matched, {len(all_entities)} entities extracted")

# ── Inspect results ────────────────────────────────────────────────────────────

# After running, `all_entities` holds all validated entity dicts.
# Example inspection:
#
#   all_entities[0]
#   [e for e in all_entities if e.get("_supertype") == "robbery_assault"]
