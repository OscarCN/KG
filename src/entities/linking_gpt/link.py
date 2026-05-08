"""Generalized entity linker with full event-linking behavior.

Events preserve the behavior from `src.entities.linking.link`. Entity
records are linked with a deliberately simple v1 strategy:
same `entity_type` + shared name token candidate retrieval, then LLM
disambiguation using only name and description.
"""

from __future__ import annotations

import copy
import logging
import random
import re
from collections import defaultdict
from dataclasses import dataclass
from typing import Any, Callable, Iterable, Literal, Optional

from src.entities.linking.geocode import geocode_location
from src.entities.linking.link_llm import disambiguate as disambiguate_event

from src.entities.linking_gpt.dates import (
    EXTRACTED_DATE_SLACK_DAYS,
    PUBLICATION_DATE_SLACK_DAYS,
    date_keys,
    parse_dt,
    resolve_event_window,
)
from src.entities.linking_gpt.entity_llm import disambiguate_entity
from src.entities.linking_gpt.schema import category_for, normalize_by_schema

logger = logging.getLogger(__name__)

LinkStatus = Literal["created", "merged", "skipped", "dropped", "error"]
Category = Literal["event", "entity", "theme"]

EventDisambiguator = Callable[[dict[str, Any], list[dict[str, Any]]], Optional[str]]
EntityDisambiguator = Callable[[dict[str, Any], list[dict[str, Any]]], Optional[str]]

ADDRESS_FIELDS = (
    "country",
    "state",
    "city",
    "neighborhood",
    "zone",
    "street",
    "number",
    "place_name",
)
ENTITY_UNION_FIELDS = ("aliases", "tags", "identifiers", "related_subjects")
ENTITY_FILL_FIELDS = ("name", "description", "context", "status")


@dataclass
class LinkResult:
    status: LinkStatus
    category: Optional[str] = None
    entity_id: Optional[str] = None
    record: Optional[dict[str, Any]] = None
    reason: Optional[str] = None


class EntityLinker:
    def __init__(
        self,
        geocode: bool = True,
        event_disambiguator: Optional[EventDisambiguator] = None,
        entity_disambiguator: Optional[EntityDisambiguator] = None,
    ):
        self.geocode = geocode
        self.event_disambiguator = event_disambiguator or disambiguate_event
        self.entity_disambiguator = entity_disambiguator or disambiguate_entity

        self.events: dict[str, dict[str, Any]] = {}
        self.entities: dict[str, dict[str, Any]] = {}
        self.dropped: dict[str, int] = defaultdict(int)

        self._event_index: dict[tuple[str, str, str], set[str]] = defaultdict(set)
        self._entity_token_index: dict[tuple[str, str], set[str]] = defaultdict(set)

    def link_all(self, records: Iterable[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
        for raw in records:
            self.link_one(raw)
        return {
            "events": list(self.events.values()),
            "entities": list(self.entities.values()),
        }

    def link_one(self, raw: dict[str, Any]) -> LinkResult:
        try:
            return self._process(raw)
        except Exception as ex:
            logger.exception("Failed to link record (%s): %s", raw.get("_supertype"), ex)
            self.dropped["error"] += 1
            return LinkResult(status="error", reason=str(ex))

    def _process(self, raw: dict[str, Any]) -> LinkResult:
        supertype = raw.get("_supertype")
        if not supertype:
            self.dropped["no_supertype"] += 1
            return LinkResult(status="dropped", reason="no_supertype")

        category = category_for(supertype)
        record = self._normalize_with_meta(raw, supertype)

        if category == "event":
            if self.geocode:
                geo = geocode_location(record.get("location"))
                if geo:
                    record["_geo"] = geo
            return self._link_event(record)

        if category == "entity":
            return self._link_entity(record)

        self.dropped[f"skipped_category:{category}"] += 1
        return LinkResult(status="skipped", category=category, reason=f"category:{category}")

    @staticmethod
    def _normalize_with_meta(raw: dict[str, Any], supertype: str) -> dict[str, Any]:
        meta = {
            "_source_id": raw.get("_source_id"),
            "_supertype": supertype,
            "date_created": raw.get("date_created"),
        }
        clean = {
            key: value
            for key, value in raw.items()
            if key not in ("_source_id", "_supertype", "date_created")
        }
        record = normalize_by_schema(clean, supertype)
        record.update({key: value for key, value in meta.items() if value is not None})
        return record

    # ------------------------------------------------------------------
    # Events
    # ------------------------------------------------------------------

    def _link_event(self, record: dict[str, Any]) -> LinkResult:
        event_type = record.get("event_type")
        if not event_type:
            self.dropped["event_no_type"] += 1
            return LinkResult(status="dropped", category="event", reason="event_no_type")

        start_dt, end_dt, slack_days, date_source = resolve_event_window(record)
        if date_source == "missing":
            self.dropped["event_no_date_no_pub"] += 1
            return LinkResult(status="dropped", category="event", reason="event_no_date_no_pub")

        level_2_id = ((record.get("_geo") or {}).get("level_2_id") or "")
        candidate_ids: set[str] = set()
        for day_key in date_keys(start_dt, end_dt, slack_days):
            candidate_ids |= self._event_index.get((event_type, level_2_id, day_key), set())

        candidates = [
            {"id": candidate_id, **_event_llm_payload(self.events[candidate_id])}
            for candidate_id in candidate_ids
        ]
        match_id = self.event_disambiguator(_event_llm_payload(record), candidates)
        if match_id and match_id in self.events:
            base = self.events[match_id]
            self._merge_event(base, record, start_dt, end_dt, date_source)
            return LinkResult(
                status="merged",
                category="event",
                entity_id=match_id,
                record=base,
            )

        new_id = self._create_event(record, start_dt, end_dt, level_2_id, date_source)
        return LinkResult(
            status="created",
            category="event",
            entity_id=new_id,
            record=self.events[new_id],
        )

    def _create_event(
        self,
        record: dict[str, Any],
        start_dt,
        end_dt,
        level_2_id: str,
        date_source: str,
    ) -> str:
        ref_dt = start_dt or end_dt
        slug = level_2_id or "noloc"
        event_id = f"{ref_dt.strftime('%Y%m%d')}_{slug}_{random.randint(100000, 999999)}"

        linked = copy.deepcopy(record)
        linked["id"] = event_id
        linked["source_ids"] = [record["_source_id"]] if record.get("_source_id") else []
        publication = record.get("date_created") or record.get("publication_date")
        if publication:
            linked["publication_date"] = publication
        linked.pop("_source_id", None)
        linked.pop("date_created", None)
        linked["_date_source"] = date_source

        self.events[event_id] = linked
        self._index_event(linked, level_2_id, start_dt, end_dt, date_source)
        return event_id

    def _merge_event(
        self,
        base: dict[str, Any],
        new: dict[str, Any],
        new_start,
        new_end,
        new_date_source: str,
    ) -> None:
        source_id = new.get("_source_id")
        if source_id and source_id not in base["source_ids"]:
            base["source_ids"].append(source_id)

        for field in ("name", "description", "context", "status"):
            if base.get(field) in (None, "", []) and new.get(field) not in (None, "", []):
                base[field] = new[field]

        new_publication = new.get("date_created") or new.get("publication_date")
        if new_publication:
            base_publication = base.get("publication_date")
            if not base_publication:
                base["publication_date"] = new_publication
            else:
                base_dt = parse_dt(base_publication)
                new_dt = parse_dt(new_publication)
                if base_dt and new_dt and new_dt < base_dt:
                    base["publication_date"] = new_publication

        base_range = base.setdefault("date_range", {}).setdefault("date_range", {})
        base_start = parse_dt(base_range.get("start"))
        base_end = parse_dt(base_range.get("end"))
        new_extracted = new_date_source == "extracted"
        if new_extracted or base_start or base_end:
            incoming_start = new_start if new_extracted else None
            incoming_end = new_end if new_extracted else None
            merged_start = (
                base_start
                if base_start and (not incoming_start or base_start <= incoming_start)
                else incoming_start
            )
            merged_end = (
                base_end
                if base_end and (not incoming_end or base_end >= incoming_end)
                else incoming_end
            )
            base_range["start"] = merged_start
            base_range["end"] = merged_end

        new_geo = new.get("_geo") or {}
        base_geo = base.get("_geo") or {}
        if new_geo.get("level_2_id") and new_geo.get("level_2_id") == base_geo.get("level_2_id"):
            new_location = new.get("location") or {}
            base_location = base.get("location") or {}
            if _populated_count(new_location) > _populated_count(base_location):
                base["location"] = new_location
                base["_geo"] = new_geo

        event_type = base.get("event_type") or ""
        level_2_id = ((base.get("_geo") or {}).get("level_2_id") or "")
        merged_start = parse_dt(base_range.get("start"))
        merged_end = parse_dt(base_range.get("end"))
        if merged_start or merged_end:
            for day_key in date_keys(merged_start, merged_end, EXTRACTED_DATE_SLACK_DAYS):
                self._event_index[(event_type, level_2_id, day_key)].add(base["id"])
        publication = parse_dt(base.get("publication_date"))
        if publication:
            for day_key in date_keys(publication, publication, PUBLICATION_DATE_SLACK_DAYS):
                self._event_index[(event_type, level_2_id, day_key)].add(base["id"])

    def _index_event(
        self,
        linked: dict[str, Any],
        level_2_id: str,
        start_dt,
        end_dt,
        date_source: str,
    ) -> None:
        event_type = linked.get("event_type") or ""
        if date_source == "extracted":
            for day_key in date_keys(start_dt, end_dt, EXTRACTED_DATE_SLACK_DAYS):
                self._event_index[(event_type, level_2_id, day_key)].add(linked["id"])
        publication = parse_dt(linked.get("publication_date"))
        if publication:
            for day_key in date_keys(publication, publication, PUBLICATION_DATE_SLACK_DAYS):
                self._event_index[(event_type, level_2_id, day_key)].add(linked["id"])

    # ------------------------------------------------------------------
    # Entities
    # ------------------------------------------------------------------

    def _link_entity(self, record: dict[str, Any]) -> LinkResult:
        entity_type = record.get("entity_type")
        if not entity_type:
            self.dropped["entity_no_type"] += 1
            return LinkResult(status="dropped", category="entity", reason="entity_no_type")

        if not record.get("name"):
            self.dropped["entity_no_name"] += 1
            return LinkResult(status="dropped", category="entity", reason="entity_no_name")

        candidate_ids = self._entity_candidate_ids(entity_type, record.get("name") or "")
        candidates = [
            {
                "id": candidate_id,
                "name": self.entities[candidate_id].get("name"),
                "description": self.entities[candidate_id].get("description") or "",
            }
            for candidate_id in candidate_ids
        ]
        incoming = {
            "name": record.get("name"),
            "description": record.get("description") or "",
        }
        match_id = self.entity_disambiguator(incoming, candidates)
        if match_id and match_id in self.entities:
            base = self.entities[match_id]
            self._merge_entity(base, record)
            return LinkResult(
                status="merged",
                category="entity",
                entity_id=match_id,
                record=base,
            )

        new_id = self._create_entity(record)
        return LinkResult(
            status="created",
            category="entity",
            entity_id=new_id,
            record=self.entities[new_id],
        )

    def _create_entity(self, record: dict[str, Any]) -> str:
        entity_type = record.get("entity_type") or "entity"
        entity_id = f"{entity_type}_{random.randint(100000, 999999)}"
        while entity_id in self.entities:
            entity_id = f"{entity_type}_{random.randint(100000, 999999)}"

        linked = copy.deepcopy(record)
        linked["id"] = entity_id
        linked["source_ids"] = [record["_source_id"]] if record.get("_source_id") else []
        publication = record.get("date_created") or record.get("publication_date")
        if publication:
            linked["publication_date"] = publication
        linked.pop("_source_id", None)
        linked.pop("date_created", None)
        self.entities[entity_id] = linked
        self._index_entity(linked)
        return entity_id

    def _merge_entity(self, base: dict[str, Any], new: dict[str, Any]) -> None:
        source_id = new.get("_source_id")
        if source_id and source_id not in base.setdefault("source_ids", []):
            base["source_ids"].append(source_id)

        had_name = bool(base.get("name"))
        for field in ENTITY_FILL_FIELDS:
            if base.get(field) in (None, "", []) and new.get(field) not in (None, "", []):
                base[field] = new[field]

        for field in ENTITY_UNION_FIELDS:
            if base.get(field) not in (None, "") or new.get(field) not in (None, ""):
                base[field] = _union_lists(base.get(field), new.get(field))

        publication = new.get("date_created") or new.get("publication_date")
        if publication and not base.get("publication_date"):
            base["publication_date"] = publication

        if not had_name and base.get("name"):
            self._index_entity(base)

    def _entity_candidate_ids(self, entity_type: str, name: str) -> set[str]:
        candidate_ids: set[str] = set()
        for token in _name_tokens(name):
            candidate_ids |= self._entity_token_index.get((entity_type, token), set())
        return candidate_ids

    def _index_entity(self, linked: dict[str, Any]) -> None:
        entity_type = linked.get("entity_type")
        if not entity_type:
            return
        for token in _name_tokens(linked.get("name") or ""):
            self._entity_token_index[(entity_type, token)].add(linked["id"])


def _event_llm_payload(record: dict[str, Any]) -> dict[str, Any]:
    publication = record.get("date_created") or record.get("publication_date")
    publication_dt = parse_dt(publication)
    return {
        "name": record.get("name"),
        "description": record.get("description") or "",
        "address": _address(record),
        "date": _date_payload(record),
        "publication_date": publication_dt.isoformat() if publication_dt else None,
    }


def _address(record: dict[str, Any]) -> dict[str, Any]:
    location = record.get("location") or {}
    return {field: location.get(field) for field in ADDRESS_FIELDS}


def _date_payload(record: dict[str, Any]) -> dict[str, Optional[str]]:
    date_range = (record.get("date_range") or {}).get("date_range") or {}
    start = parse_dt(date_range.get("start"))
    end = parse_dt(date_range.get("end"))
    return {
        "start": start.isoformat() if start else None,
        "end": end.isoformat() if end else None,
    }


def _populated_count(location: dict[str, Any]) -> int:
    if not isinstance(location, dict):
        return 0
    return sum(1 for field in ADDRESS_FIELDS if location.get(field))


def _name_tokens(name: str) -> set[str]:
    return {
        token
        for token in re.split(r"\W+", str(name).lower(), flags=re.UNICODE)
        if len(token) >= 3
    }


def _union_lists(base_value: Any, new_value: Any) -> list[Any]:
    out: list[Any] = []
    seen: set[str] = set()
    for value in _as_list(base_value) + _as_list(new_value):
        key = repr(value)
        if key in seen:
            continue
        seen.add(key)
        out.append(value)
    return out


def _as_list(value: Any) -> list[Any]:
    if value in (None, ""):
        return []
    if isinstance(value, list):
        return value
    return [value]
