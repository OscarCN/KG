"""Streaming event linking step."""

from __future__ import annotations

import os
from typing import Optional, Protocol

from src.entities.tags_gpt.candidates import EventCandidateFinder, parse_day
from src.entities.tags_gpt.catalogs import EventStore
from src.entities.tags_gpt.llm import JsonLlm
from src.entities.tags_gpt.models import EventMention, LinkResult, LinkedEvent
from src.entities.tags_gpt.prompts import link_prompt


class LinkDecider(Protocol):
    def choose_match(self, incoming: EventMention, candidates: list[LinkedEvent]) -> Optional[str]: ...


class NoMatchDecider:
    def choose_match(self, incoming: EventMention, candidates: list[LinkedEvent]) -> Optional[str]:
        return None


class ExactTitleDecider:
    """Deterministic test decider.

    It links when normalized names match exactly. Use an LLM decider in
    production when candidates need semantic disambiguation.
    """

    def choose_match(self, incoming: EventMention, candidates: list[LinkedEvent]) -> Optional[str]:
        name = (incoming.name or "").strip().lower()
        if not name:
            return None
        for candidate in candidates:
            if (candidate.name or "").strip().lower() == name:
                return candidate.id
        return None


class LlmLinkDecider:
    def __init__(self, llm: JsonLlm, *, model: Optional[str] = None):
        self.llm = llm
        self.model = model or os.environ.get("OPENROUTER_LINKER_MODEL", "google/gemini-2.5-flash-lite")

    def choose_match(self, incoming: EventMention, candidates: list[LinkedEvent]) -> Optional[str]:
        if not candidates:
            return None
        incoming_payload = {
            "name": incoming.name,
            "description": incoming.description,
            "address": incoming.location,
            "date": {"start": incoming.date_start, "end": incoming.date_end},
            "publication_date": incoming.publication_date,
        }
        candidate_payloads = [
            {
                "id": candidate.id,
                "name": candidate.name,
                "description": candidate.description,
                "address": candidate.location,
                "date": {"start": candidate.date_start, "end": candidate.date_end},
                "publication_date": candidate.publication_date,
            }
            for candidate in candidates
        ]
        payload = {"incoming": incoming_payload, "candidates": candidate_payloads}
        response = self.llm.complete_json(
            phase="event_linking",
            payload=payload,
            prompt=link_prompt(incoming_payload, candidate_payloads),
            model=self.model,
        )
        match_id = response.get("match_id")
        valid_ids = {candidate.id for candidate in candidates}
        match_id_str = str(match_id) if match_id is not None else None
        return match_id_str if match_id_str in valid_ids else None


class EventLinkingStep:
    def __init__(
        self,
        event_store: Optional[EventStore] = None,
        candidate_finder: Optional[EventCandidateFinder] = None,
        decider: Optional[LinkDecider] = None,
    ):
        self.event_store = event_store or EventStore()
        self.candidate_finder = candidate_finder or EventCandidateFinder(self.event_store)
        self.decider = decider or NoMatchDecider()
        self._counter = 0

    def link_record(self, record: dict) -> LinkResult:
        mention = EventMention.from_record(record)
        if not mention.is_event:
            return LinkResult(status="skipped", reason=f"not_event:{mention.supertype}")
        if not mention.date_start:
            return LinkResult(status="dropped", reason="event_without_date")

        candidates = self.candidate_finder.candidates_for(mention)
        match_id = self.decider.choose_match(mention, candidates)
        if match_id and self.event_store.get(match_id):
            event = self.event_store.get(match_id)
            assert event is not None
            event.merge(mention)
            return LinkResult(status="merged", event_id=event.id, event=event)

        event = LinkedEvent.from_mention(self._new_id(mention), mention)
        self.event_store.add(event)
        return LinkResult(status="created", event_id=event.id, event=event)

    def _new_id(self, mention: EventMention) -> str:
        self._counter += 1
        day = parse_day(mention.date_start)
        date_part = day.strftime("%Y%m%d") if day else "nodate"
        area = mention.level_2_id or "noloc"
        return f"{date_part}_{area}_{self._counter:06d}"
