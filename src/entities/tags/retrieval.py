"""Article-bundle retrieval over the pre-linked fixture.

Reads the two-file layout produced by `scripts/build_linked_fixture.py`:

- `<linked_path>` — list of news documents enriched with `event_ids`.
- `<events_path>` — flat dict keyed by event_id → `LinkedEventContext`.

Each yielded `ArticleBundle` has a `root` (the article/post), a list of
`comments`, the document's `event_ids`, and resolved `linked_events` blocks
ready for prompt context.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Iterator, Optional

from src.entities.tags.models import (
    ArticleBundle,
    Customer,
    LinkedEventContext,
    SourceItem,
)


logger = logging.getLogger(__name__)


_ARTICLE_NEWS_TYPES = frozenset({"news", "newspaper", "blog", "article"})


def _kind_for(doc: dict[str, Any]) -> str:
    nt = (doc.get("news_type") or "").lower()
    if nt in _ARTICLE_NEWS_TYPES:
        return "article"
    if nt in ("facebook", "twitter", "x", "instagram", "tiktok", "social"):
        return "user_post"
    # default: treat as article (news-like)
    return "article"


def _root_text(doc: dict[str, Any]) -> str:
    title = (doc.get("title") or "").strip()
    body = (doc.get("text") or "").strip()
    if title and body:
        return f"{title}\n\n{body}"
    return title or body


def _root_author(doc: dict[str, Any]) -> Optional[str]:
    a = doc.get("author_name")
    if isinstance(a, list):
        a = next((x for x in a if x), None)
    return a if isinstance(a, str) and a else None


def _document_to_bundle(
    doc: dict[str, Any],
    *,
    events_index: dict[str, dict[str, Any]],
    customer: Optional[Customer],
) -> Optional[ArticleBundle]:
    url = doc.get("url")
    if not url:
        logger.warning("document missing `url`; skipping")
        return None

    root = SourceItem(
        id=url,
        kind=_kind_for(doc),
        text=_root_text(doc),
        author=_root_author(doc),
        created_at=doc.get("date_created"),
        parent_source_id=None,
        metadata={
            k: v
            for k, v in doc.items()
            if k not in {"url", "title", "text", "comments", "event_ids"}
        },
    )

    comments_raw = doc.get("comments") or []
    comments: list[SourceItem] = []
    for c in comments_raw:
        cid = c.get("comment_id") or f"{url}#{len(comments)}"
        comments.append(
            SourceItem(
                id=str(cid),
                kind="user_comment",
                text=c.get("comment_text") or "",
                author=c.get("comment_author"),
                created_at=c.get("comment_timestamp"),
                parent_source_id=url,
                metadata={
                    k: v for k, v in c.items()
                    if k not in {"comment_id", "comment_text", "comment_author", "comment_timestamp"}
                },
            )
        )

    event_ids = list(doc.get("event_ids") or [])
    linked_events: list[LinkedEventContext] = []
    for ev_id in event_ids:
        raw = events_index.get(ev_id)
        if raw is None:
            logger.debug("event_id %s referenced by %s not in events store", ev_id, url)
            continue
        linked_events.append(LinkedEventContext.from_dict(raw))

    return ArticleBundle(
        root=root,
        comments=comments,
        event_ids=event_ids,
        linked_events=linked_events,
        customer=customer,
    )


class ArticleBundleRetriever:
    """Reads a pre-linked fixture and yields `ArticleBundle`s."""

    def __init__(
        self,
        linked_path: Path,
        events_path: Path,
        customer: Optional[Customer] = None,
    ):
        self.linked_path = linked_path
        self.events_path = events_path
        self.customer = customer
        self._docs: Optional[list[dict[str, Any]]] = None
        self._events_index: Optional[dict[str, dict[str, Any]]] = None

    # ── Lazy loading ───────────────────────────────────────────────────

    def _ensure_loaded(self) -> None:
        if self._docs is None:
            with open(self.linked_path, encoding="utf-8") as f:
                docs = json.load(f)
            if not isinstance(docs, list):
                raise ValueError(
                    f"{self.linked_path}: expected a list of documents, "
                    f"got {type(docs).__name__}"
                )
            self._docs = docs
        if self._events_index is None:
            if self.events_path.exists():
                with open(self.events_path, encoding="utf-8") as f:
                    self._events_index = json.load(f) or {}
            else:
                logger.warning("events file %s missing — bundles will lack linked_events",
                               self.events_path)
                self._events_index = {}

    # ── Public API ─────────────────────────────────────────────────────

    def iter_bundles(self) -> Iterator[ArticleBundle]:
        self._ensure_loaded()
        assert self._docs is not None and self._events_index is not None
        for doc in self._docs:
            bundle = _document_to_bundle(
                doc, events_index=self._events_index, customer=self.customer
            )
            if bundle is not None:
                yield bundle

    def bundle_for(self, source_id: str) -> Optional[ArticleBundle]:
        self._ensure_loaded()
        assert self._docs is not None and self._events_index is not None
        for doc in self._docs:
            if doc.get("url") == source_id:
                return _document_to_bundle(
                    doc, events_index=self._events_index, customer=self.customer
                )
        return None

    def __len__(self) -> int:
        self._ensure_loaded()
        return len(self._docs or [])

    def event_descriptions(self) -> dict[str, str]:
        """Map of event_id → description (used by stats / consistency)."""
        self._ensure_loaded()
        assert self._events_index is not None
        return {ev_id: (raw.get("description") or "") for ev_id, raw in self._events_index.items()}
