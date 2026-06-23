"""Map an incoming document envelope to the article dict the extractor expects.

Shared by the streaming listener (``src/listener.py``) and the local stream
simulation (``src/entities/run_entities.py``). Supports two record shapes:

- Facebook-style records with a nested ``message`` dict.
- News-style ES hits with flat ``text``, ``title``, ``url``, etc.
"""

from __future__ import annotations

from typing import Any, Dict


def record_to_article(record: Dict[str, Any]) -> Dict[str, Any]:
    """Normalize one raw document into the extractor's article payload."""
    msg = record.get("message")
    if isinstance(msg, dict):
        body = msg.get("body", "") or ""
        title = msg.get("title", "") or ""
        url = msg.get("url") or record.get("_id") or ""
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
        doc_type = record.get("doctype") or record.get("type") or "news"
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

    source_id = str(record.get("_id") or url or id(record))
    return {
        "id": source_id,
        "text": body,
        "title": title,
        "url": url,
        "categories": categories,
        "document_type": doc_type,
        "publication_date": publication_date,
    }
