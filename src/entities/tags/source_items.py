"""Source-item fetchers for the consistency pass.

When the streaming worker restarts, the in-memory `items_seen` dict is
empty. The consistency pass's Stages 2 and 3 still need the raw text of
the items inside the recent-bundle window — that's what these fetchers
provide.

Two implementations:
- `LocalFileSourceItemFetcher` — re-reads the linked.json fixture used by
  `ArticleBundleRetriever`. Used while the consumer is still
  file-simulated.
- `ESSourceItemFetcher` — queries Elasticsearch's `news` index by `url`.
  Comments are embedded on each parent post (`doc.comments[…]`); a
  single ES round-trip per consistency pass recovers everything.

Both expose `fetch_for_assignments(assignments)` which collects the IDs
from a window of `StanceAssignment` rows, looks up the corresponding
posts (and their comments), and returns a flat `dict[source_item_id ->
SourceItem]` shaped exactly like the in-memory `items_seen`.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Iterable, Optional, Protocol

from src.entities.tags.models import SourceItem, StanceAssignment


logger = logging.getLogger(__name__)


_ARTICLE_NEWS_TYPES = frozenset({"news", "newspaper", "blog", "article"})


def _kind_for(news_type: Optional[str]) -> str:
    nt = (news_type or "").lower()
    if nt in _ARTICLE_NEWS_TYPES:
        return "article"
    if nt in ("facebook", "twitter", "x", "instagram", "tiktok", "linkedin", "social"):
        return "user_post"
    return "article"


def _post_text(doc: dict[str, Any]) -> str:
    title = (doc.get("title") or "").strip()
    body = (doc.get("text") or "").strip()
    if title and body:
        return f"{title}\n\n{body}"
    return title or body


def _post_to_items(doc: dict[str, Any]) -> dict[str, SourceItem]:
    """Expand a news-index document into post + comments, keyed by id."""
    url = doc.get("url")
    if not url:
        return {}
    news_type = doc.get("news_type")
    out: dict[str, SourceItem] = {
        url: SourceItem(
            id=url,
            kind=_kind_for(news_type),
            text=_post_text(doc),
            author=(doc.get("author_name") if isinstance(doc.get("author_name"), str)
                    else (doc.get("author_name") or [None])[0]),
            created_at=doc.get("date_created"),
            parent_source_id=None,
            metadata={k: v for k, v in doc.items()
                      if k not in {"url", "title", "text", "comments"}},
        )
    }
    for c in doc.get("comments") or []:
        cid = c.get("comment_id")
        if not cid:
            continue
        out[str(cid)] = SourceItem(
            id=str(cid),
            kind="user_comment",
            text=c.get("comment_text") or "",
            author=c.get("comment_author"),
            created_at=c.get("comment_timestamp"),
            parent_source_id=url,
            metadata={k: v for k, v in c.items()
                      if k not in {"comment_id", "comment_text", "comment_author",
                                   "comment_timestamp"}},
        )
    return out


def _post_urls_from_assignments(
    assignments: Iterable[StanceAssignment],
) -> set[str]:
    """Pull the post URLs referenced by a window of assignments.

    For posts/articles the URL is `source_item_id`. For comments the URL
    is the comment's `parent_source_id`."""
    urls: set[str] = set()
    for a in assignments:
        if a.source_kind in ("article", "user_post"):
            urls.add(a.source_item_id)
        elif a.source_kind == "user_comment" and a.parent_source_id:
            urls.add(a.parent_source_id)
    return urls


# ── Protocol ──────────────────────────────────────────────────────────


class SourceItemFetcher(Protocol):
    def fetch_for_assignments(
        self, assignments: Iterable[StanceAssignment],
    ) -> dict[str, SourceItem]: ...


# ── Local-file implementation ─────────────────────────────────────────


class LocalFileSourceItemFetcher:
    """Fetcher backed by the same `linked.json` fixture the retriever uses.

    Used while `stream.py` is still file-simulated. Reads the file once
    on first use and serves all subsequent lookups from a flat in-memory
    index keyed by `source_item_id`.
    """

    def __init__(self, linked_path: Path):
        self.linked_path = Path(linked_path)
        self._index: Optional[dict[str, SourceItem]] = None

    def _ensure_loaded(self) -> None:
        if self._index is not None:
            return
        with open(self.linked_path, encoding="utf-8") as f:
            docs = json.load(f)
        flat: dict[str, SourceItem] = {}
        for doc in docs:
            flat.update(_post_to_items(doc))
        self._index = flat
        logger.info(
            "LocalFileSourceItemFetcher: loaded %d items from %s",
            len(flat), self.linked_path,
        )

    def fetch_for_assignments(
        self, assignments: Iterable[StanceAssignment],
    ) -> dict[str, SourceItem]:
        self._ensure_loaded()
        assert self._index is not None
        # We don't need the post-URL set explicitly because we already
        # have a full id index — just resolve every assignment's id.
        wanted: set[str] = set()
        for a in assignments:
            wanted.add(a.source_item_id)
            if a.source_kind == "user_comment" and a.parent_source_id:
                wanted.add(a.parent_source_id)
        return {sid: self._index[sid] for sid in wanted if sid in self._index}


# ── Elasticsearch implementation ──────────────────────────────────────


class ESSourceItemFetcher:
    """Fetcher backed by the Elasticsearch `news` index.

    Issues one `terms` query on `url` per call — comments come embedded
    on each parent post (`doc.comments[…]`). Falls through to an empty
    result on connection / query failure (the consistency pass
    degrades to using only `assignment.reason` for Stage 2 / 3 input).

    The class is intentionally light on dependencies: it imports
    `elasticsearch_dsl.Search` lazily inside the fetch call so that
    importing this module does not require the `elastic_client` sibling
    package on the path. Call sites that wire ES in production must
    register the connection (typically via `SearchClient()`'s
    constructor in `elastic_client.connection`).
    """

    def __init__(
        self,
        *,
        index: str = "news",
        connection_alias: str = "medios3conn",
        page_size: int = 500,
    ):
        self.index = index
        self.connection_alias = connection_alias
        self.page_size = page_size

    def fetch_for_assignments(
        self, assignments: Iterable[StanceAssignment],
    ) -> dict[str, SourceItem]:
        post_urls = _post_urls_from_assignments(assignments)
        if not post_urls:
            return {}
        try:
            return self._fetch_by_urls(sorted(post_urls))
        except Exception as exc:  # pragma: no cover — defensive
            logger.warning(
                "ESSourceItemFetcher: query failed (%s); falling back to empty",
                exc,
            )
            return {}

    def _fetch_by_urls(self, urls: list[str]) -> dict[str, SourceItem]:
        from elasticsearch_dsl import Search  # lazy import

        flat: dict[str, SourceItem] = {}
        for batch_start in range(0, len(urls), self.page_size):
            batch = urls[batch_start: batch_start + self.page_size]
            s = (
                Search(using=self.connection_alias, index=self.index)
                .filter("terms", url=batch)
                .source([
                    "url", "title", "text", "author_name", "date_created",
                    "comments", "news_type", "supplier",
                    "source", "event_ids",
                ])
                .extra(size=len(batch))
            )
            response = s.execute()
            for hit in response.hits:
                flat.update(_post_to_items(hit.to_dict()))
        return flat
