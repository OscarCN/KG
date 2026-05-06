"""Retrieval — fetch articles + their comments by `_source_id` (URL).

Stage-1 implementation reads from the ES `news` index and parses the
embedded `comments` array on each article doc into `SourceItem` rows.
Posts (a separate index, social-media native) are not wired yet —
`get_post_comments` raises `NotImplementedError`.

Responses are cached on disk under `cache/es_articles/<sha256>.json`
so re-runs don't re-hit ES.
"""

from __future__ import annotations

import hashlib
import json
import logging
from pathlib import Path
from typing import Iterable, Optional

from src.entities.tags.models.source_item import SourceItem

logger = logging.getLogger(__name__)


_PROJECT_ROOT = Path(__file__).resolve().parents[3]
_DEFAULT_CACHE_DIR = _PROJECT_ROOT / "cache" / "es_articles"
_DEFAULT_INDEX = "news"

_ARTICLE_FIELDS = (
    "url",
    "title",
    "text",
    "summary",
    "author_name",
    "source",
    "date_created",
    "comments",
)


def _key_for_source_id(source_id: str) -> str:
    return hashlib.sha256(source_id.encode("utf-8")).hexdigest()


class Retrieval:
    """ES wrapper for the tags pipeline.

    The constructor accepts an injected `SearchClient` so tests / scripts
    can pass a stub. The default factory imports `elastic_client.SearchClient`
    lazily so the module can be imported without ES credentials.
    """

    def __init__(
        self,
        search_client=None,
        news_index: str = _DEFAULT_INDEX,
        cache_dir: Optional[Path] = None,
    ):
        self._client = search_client
        self.news_index = news_index
        self.cache_dir = cache_dir or _DEFAULT_CACHE_DIR

    # ── ES access ──────────────────────────────────────────────────

    def _ensure_client(self):
        if self._client is None:
            from elastic_client import SearchClient  # type: ignore

            self._client = SearchClient()
        return self._client

    def _fetch_article_doc(self, source_id: str) -> Optional[dict]:
        cached = self._cache_read(source_id)
        if cached is not None:
            # Cached `null` means "we looked, ES had nothing".
            return cached or None

        client = self._ensure_client()
        s = client.raw_search(self.news_index)
        s = s.filter("terms", url=[source_id])
        s = s.source(list(_ARTICLE_FIELDS))
        s = s.extra(size=1)
        try:
            response = s.execute()
        except Exception as ex:  # pragma: no cover — passes through
            logger.warning("ES fetch failed for %s: %s", source_id, ex)
            return None

        hits = list(response)
        doc = hits[0].to_dict() if hits else None
        self._cache_write(source_id, doc)
        return doc

    # ── public API ─────────────────────────────────────────────────

    def get_article(self, source_id: str) -> Optional[SourceItem]:
        doc = self._fetch_article_doc(source_id)
        if not doc:
            return None
        return self._article_doc_to_item(doc)

    def get_article_with_comments(
        self, source_id: str
    ) -> tuple[Optional[SourceItem], list[SourceItem]]:
        doc = self._fetch_article_doc(source_id)
        if not doc:
            return None, []
        article = self._article_doc_to_item(doc)
        comments = self._comments_from_doc(doc, parent_url=source_id)
        return article, comments

    def get_event_items(
        self,
        event_id: str,
        source_ids: Iterable[str],
    ) -> list[SourceItem]:
        seen: set[str] = set()
        items: list[SourceItem] = []
        for sid in source_ids:
            article, comments = self.get_article_with_comments(sid)
            if article is not None and article.id not in seen:
                items.append(article)
                seen.add(article.id)
            for c in comments:
                if c.id not in seen:
                    items.append(c)
                    seen.add(c.id)
        return items

    def get_post_comments(self, post_id: str) -> list[SourceItem]:
        raise NotImplementedError(
            "Posts/social index not wired yet. Stage 1 reads from the news "
            "index only — see src/entities/tags/readme_tags.md."
        )

    def get_customer_corpus(
        self,
        source_ids: Iterable[str],
        limit: Optional[int] = None,
    ) -> list[SourceItem]:
        """Bootstrap-time corpus loader: turns a list of source_ids
        (typically the URLs in the already-extracted records) into a
        flat SourceItem list mixing articles and their comments.
        """
        out: list[SourceItem] = []
        for i, sid in enumerate(source_ids):
            if limit and len(out) >= limit:
                break
            article, comments = self.get_article_with_comments(sid)
            if article is not None:
                out.append(article)
            out.extend(comments)
        return out

    # ── parsing ────────────────────────────────────────────────────

    @staticmethod
    def _article_doc_to_item(doc: dict) -> SourceItem:
        url = doc.get("url") or doc.get("_id") or ""
        text_parts = [doc.get("title"), doc.get("text") or doc.get("summary")]
        text = "\n\n".join(p for p in text_parts if p)
        return SourceItem(
            id=url,
            kind="article",
            text=text,
            author=doc.get("author_name"),
            created_at=doc.get("date_created"),
            parent_source_id=None,
            metadata={
                "source": doc.get("source"),
                "fb_likes": doc.get("fb_likes"),
            },
        )

    @staticmethod
    def _comments_from_doc(doc: dict, parent_url: str) -> list[SourceItem]:
        comments = doc.get("comments") or []
        out: list[SourceItem] = []
        for c in comments:
            text = (c.get("comment_text") or "").strip()
            if not text:
                continue
            cid = c.get("comment_id") or f"{parent_url}#{len(out)}"
            out.append(
                SourceItem(
                    id=cid,
                    kind="user_comment",
                    text=text,
                    author=c.get("comment_author"),
                    created_at=c.get("comment_timestamp"),
                    parent_source_id=parent_url,
                    metadata={"likes": c.get("comment_likes")},
                )
            )
        return out

    # ── cache ──────────────────────────────────────────────────────

    def _cache_read(self, source_id: str) -> Optional[dict]:
        path = self.cache_dir / f"{_key_for_source_id(source_id)}.json"
        if not path.exists():
            return None
        try:
            with open(path, encoding="utf-8") as f:
                return json.load(f)
        except (OSError, json.JSONDecodeError):
            return None

    def _cache_write(self, source_id: str, doc: Optional[dict]) -> None:
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        path = self.cache_dir / f"{_key_for_source_id(source_id)}.json"
        with open(path, "w", encoding="utf-8") as f:
            json.dump(doc, f, ensure_ascii=False)


class LocalFileRetrieval(Retrieval):
    """Stage-1 testing helper — reads articles from a local JSON file
    instead of ES. Useful for the streaming runner when ES isn't
    reachable. The file format is a list of news docs with the same
    field shape ES returns (matches `data/ayuntamiento_qro/*.json`).
    """

    def __init__(self, news_json_path: Path, cache_dir: Optional[Path] = None):
        super().__init__(search_client=None, cache_dir=cache_dir)
        with open(news_json_path, encoding="utf-8") as f:
            docs = json.load(f)
        self._by_url: dict[str, dict] = {}
        for d in docs:
            url = d.get("url")
            if url:
                self._by_url[url] = d

    def _fetch_article_doc(self, source_id: str) -> Optional[dict]:
        return self._by_url.get(source_id)
