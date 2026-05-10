"""Load pre-linked article bundles for tags_gpt."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Iterable, Protocol

from src.entities.tags_gpt.models import (
    ArticleBundle,
    Customer,
    LinkedEventContext,
    SourceItem,
)


class ArticleBundleRetriever(Protocol):
    def iter_bundles(self, customer: Customer) -> Iterable[ArticleBundle]: ...


def load_event_contexts(path: Path) -> dict[str, LinkedEventContext]:
    if not path.exists():
        return {}
    with open(path, encoding="utf-8") as handle:
        raw = json.load(handle)
    if isinstance(raw, list):
        return {
            str(item.get("id")): LinkedEventContext.from_dict(str(item.get("id")), item)
            for item in raw
            if isinstance(item, dict) and item.get("id")
        }
    return {
        str(event_id): LinkedEventContext.from_dict(str(event_id), value)
        for event_id, value in (raw or {}).items()
    }


def doc_to_root_item(doc: dict) -> SourceItem:
    source_id = str(doc.get("url") or doc.get("id") or doc.get("_id") or "")
    text = "\n\n".join(
        str(value)
        for value in (doc.get("title"), doc.get("text") or doc.get("summary"))
        if value
    )
    kind = "user_post" if doc.get("kind") == "user_post" or doc.get("source_type") == "user_post" else "article"
    return SourceItem(
        id=source_id,
        kind=kind,
        text=text,
        author=doc.get("author") or doc.get("author_name"),
        created_at=doc.get("created_at") or doc.get("date_created"),
        metadata={key: doc.get(key) for key in ("source", "fb_likes") if key in doc},
    )


def comments_from_doc(doc: dict, parent_source_id: str) -> list[SourceItem]:
    comments: list[SourceItem] = []
    for index, comment in enumerate(doc.get("comments") or []):
        text = str(comment.get("text") or comment.get("comment_text") or "").strip()
        if not text:
            continue
        comments.append(
            SourceItem(
                id=str(comment.get("id") or comment.get("comment_id") or f"{parent_source_id}#comment-{index}"),
                kind="user_comment",
                text=text,
                author=comment.get("author") or comment.get("comment_author"),
                created_at=comment.get("created_at") or comment.get("comment_timestamp"),
                parent_source_id=parent_source_id,
                metadata={"likes": comment.get("likes") or comment.get("comment_likes")},
            )
        )
    return comments


class LinkedJsonRetriever:
    def __init__(self, corpus_path: Path, events_path: Path | None = None):
        self.corpus_path = corpus_path
        self.events_path = events_path or corpus_path.with_name(f"{corpus_path.stem}__events.json")
        with open(corpus_path, encoding="utf-8") as handle:
            raw = json.load(handle)
        self.docs = raw if isinstance(raw, list) else raw.get("documents", [])
        self.events = load_event_contexts(self.events_path)

    def iter_bundles(self, customer: Customer) -> Iterable[ArticleBundle]:
        for doc in self.docs:
            if not isinstance(doc, dict):
                continue
            root = doc_to_root_item(doc)
            if not root.id:
                continue
            event_ids = [str(x) for x in doc.get("event_ids") or []]
            yield ArticleBundle(
                root=root,
                comments=comments_from_doc(doc, root.id),
                event_ids=event_ids,
                linked_events=[self.events[event_id] for event_id in event_ids if event_id in self.events],
                customer=customer,
            )

    def get_customer_corpus(self, customer: Customer, limit: int | None = None) -> list[SourceItem]:
        out: list[SourceItem] = []
        for bundle in self.iter_bundles(customer):
            out.extend(bundle.items)
            if limit is not None and len(out) >= limit:
                return out[:limit]
        return out

