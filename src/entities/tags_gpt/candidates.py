"""Candidate retrieval for streaming event linking."""

from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Optional

from src.entities.tags_gpt.catalogs import EventStore
from src.entities.tags_gpt.models import EventMention, LinkedEvent


def parse_day(value: Optional[str]) -> Optional[date]:
    if not value:
        return None
    text = str(value)
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).date()
    except ValueError:
        try:
            return date.fromisoformat(text[:10])
        except ValueError:
            return None


def ranges_overlap(
    start_a: Optional[str],
    end_a: Optional[str],
    start_b: Optional[str],
    end_b: Optional[str],
    *,
    slack_days: int = 2,
) -> bool:
    a0 = parse_day(start_a)
    b0 = parse_day(start_b)
    if not a0 or not b0:
        return False
    a1 = parse_day(end_a) or a0
    b1 = parse_day(end_b) or b0
    return (a0 - timedelta(days=slack_days)) <= b1 and (b0 - timedelta(days=slack_days)) <= a1


class EventCandidateFinder:
    """Broad candidate retrieval.

    It intentionally only retrieves plausible candidates. The final same
    vs different decision belongs to a link decider.
    """

    def __init__(self, event_store: EventStore, *, slack_days: int = 2):
        self.event_store = event_store
        self.slack_days = slack_days

    def candidates_for(self, mention: EventMention) -> list[LinkedEvent]:
        out: list[LinkedEvent] = []
        for event in self.event_store.values():
            if event.event_type != mention.event_type:
                continue
            if event.level_2_id != mention.level_2_id:
                continue
            if not ranges_overlap(
                mention.date_start,
                mention.date_end,
                event.date_start,
                event.date_end,
                slack_days=self.slack_days,
            ):
                continue
            out.append(event)
        return out
