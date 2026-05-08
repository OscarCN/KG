"""Adapter from linking_gpt to the tags_gpt streaming interface."""

from __future__ import annotations

from typing import Optional

from src.entities.linking_gpt.link import EntityLinker
from src.entities.tags_gpt.catalogs import EventStore
from src.entities.tags_gpt.models import LinkResult as TagsLinkResult
from src.entities.tags_gpt.models import LinkedEvent


class TagsGptLinkingAdapter:
    """Expose `link_record()` in the shape expected by tags_gpt streaming.

    The generalized linker links both events and entities. The tags
    pipeline only tags events for now, so entity results are retained on
    `self.linker.entities` but returned as `skipped` to the tags stream.
    """

    def __init__(
        self,
        event_store: Optional[EventStore] = None,
        linker: Optional[EntityLinker] = None,
        *,
        geocode: bool = True,
    ):
        self.event_store = event_store or EventStore()
        self.linker = linker or EntityLinker(geocode=geocode)

    def link_record(self, record: dict) -> TagsLinkResult:
        result = self.linker.link_one(record)
        if result.category == "event" and result.status in ("created", "merged") and result.record:
            event = _linked_event_from_record(result.record)
            self.event_store.add(event)
            return TagsLinkResult(status=result.status, event_id=event.id, event=event, reason=result.reason)

        if result.category == "entity" and result.status in ("created", "merged"):
            return TagsLinkResult(status="skipped", reason="category:entity")

        return TagsLinkResult(status=result.status, reason=result.reason)


def _linked_event_from_record(record: dict) -> LinkedEvent:
    return LinkedEvent(
        id=str(record.get("id") or ""),
        event_type=str(record.get("event_type") or ""),
        name=str(record.get("name") or ""),
        description=str(record.get("description") or ""),
        source_ids=list(record.get("source_ids") or []),
        date_range=dict(record.get("date_range") or {}),
        location=dict(record.get("location") or {}),
        publication_date=record.get("publication_date"),
        raw=dict(record),
    )
