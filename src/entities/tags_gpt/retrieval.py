"""Content retrieval step.

The production retriever can hit Elasticsearch, while tests can use
`LocalJsonRetriever` or any object implementing `get_article_bundle`.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Iterable, Optional, Protocol

from src.entities.tags_gpt.models import ArticleBundle, SourceItem


NEWS_FIELDS = (
    "url",
    "title",
    "text",
    "summary",
    "author_name",
    "source",
    "date_created",
    "comments",
)


class ContentRetriever(Protocol):
    def get_article_bundle(self, source_id: str) -> ArticleBundle: ...

    def get_customer_corpus(
        self, source_ids: Iterable[str], limit: Optional[int] = None
    ) -> list[SourceItem]: ...


def article_doc_to_item(doc: dict) -> SourceItem:
    url = str(doc.get("url") or doc.get("_id") or "")
    text = "\n\n".join(
        value for value in [doc.get("title"), doc.get("text") or doc.get("summary")] if value
    )
    return SourceItem(
        id=url,
        kind="article",
        text=text,
        author=doc.get("author_name"),
        created_at=doc.get("date_created"),
        metadata={"source": doc.get("source"), "fb_likes": doc.get("fb_likes")},
    )


def comments_from_doc(doc: dict, parent_source_id: str) -> list[SourceItem]:
    out: list[SourceItem] = []
    for i, comment in enumerate(doc.get("comments") or []):
        text = str(comment.get("comment_text") or "").strip()
        if not text:
            continue
        out.append(
            SourceItem(
                id=str(comment.get("comment_id") or f"{parent_source_id}#comment-{i}"),
                kind="user_comment",
                text=text,
                author=comment.get("comment_author"),
                created_at=comment.get("comment_timestamp"),
                parent_source_id=parent_source_id,
                metadata={"likes": comment.get("comment_likes")},
            )
        )
    return out


class LocalJsonRetriever:
    def __init__(self, news_json_path: Path):
        with open(news_json_path, encoding="utf-8") as handle:
            docs = json.load(handle)
        self.docs_by_url = {
            str(doc["url"]): dict(doc)
            for doc in docs
            if isinstance(doc, dict) and doc.get("url")
        }

    def get_article_bundle(self, source_id: str) -> ArticleBundle:
        doc = self.docs_by_url.get(source_id)
        if not doc:
            return ArticleBundle(source_id=source_id)
        return ArticleBundle(
            source_id=source_id,
            article=article_doc_to_item(doc),
            comments=comments_from_doc(doc, source_id),
        )

    def get_customer_corpus(
        self, source_ids: Iterable[str], limit: Optional[int] = None
    ) -> list[SourceItem]:
        out: list[SourceItem] = []
        for source_id in source_ids:
            if limit is not None and len(out) >= limit:
                break
            out.extend(self.get_article_bundle(source_id).items)
        return out[:limit] if limit is not None else out


class EsNewsRetriever:
    def __init__(
        self,
        search_client=None,
        *,
        news_index: str = "news",
        cache_dir: Optional[Path] = None,
    ):
        self.search_client = search_client
        self.news_index = news_index
        self.cache_dir = cache_dir

    def get_article_bundle(self, source_id: str) -> ArticleBundle:
        doc = self._get_doc(source_id)
        if not doc:
            return ArticleBundle(source_id=source_id)
        return ArticleBundle(
            source_id=source_id,
            article=article_doc_to_item(doc),
            comments=comments_from_doc(doc, source_id),
        )

    def get_customer_corpus(
        self, source_ids: Iterable[str], limit: Optional[int] = None
    ) -> list[SourceItem]:
        out: list[SourceItem] = []
        for source_id in source_ids:
            if limit is not None and len(out) >= limit:
                break
            out.extend(self.get_article_bundle(source_id).items)
        return out[:limit] if limit is not None else out

    def _client(self):
        if self.search_client is None:
            from elastic_client import SearchClient  # type: ignore

            self.search_client = SearchClient()
        return self.search_client

    def _get_doc(self, source_id: str) -> Optional[dict]:
        cached = self._cache_read(source_id)
        if cached is not None:
            return cached or None

        query = self._client().raw_search(self.news_index)
        query = query.filter("terms", url=[source_id])
        query = query.source(list(NEWS_FIELDS))
        query = query.extra(size=1)
        hits = list(query.execute())
        doc = hits[0].to_dict() if hits else None
        self._cache_write(source_id, doc)
        return doc

    def _cache_path(self, source_id: str) -> Optional[Path]:
        if not self.cache_dir:
            return None
        key = hashlib.sha256(source_id.encode("utf-8")).hexdigest()
        return self.cache_dir / f"{key}.json"

    def _cache_read(self, source_id: str) -> Optional[dict]:
        path = self._cache_path(source_id)
        if not path or not path.exists():
            return None
        with open(path, encoding="utf-8") as handle:
            return json.load(handle)

    def _cache_write(self, source_id: str, doc: Optional[dict]) -> None:
        path = self._cache_path(source_id)
        if not path:
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(doc, handle, ensure_ascii=False)
