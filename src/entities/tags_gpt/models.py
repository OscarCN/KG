"""Pure data models for the decoupled tags pipeline.

These classes intentionally contain no LLM calls, filesystem access, or
database access. They are small enough to use directly in unit tests and
specific enough to describe the boundaries between the streaming steps.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from statistics import median
from typing import Any, Literal, Optional


SourceKind = Literal["article", "user_post", "user_comment"]
LinkStatus = Literal["created", "merged", "skipped", "dropped", "error"]


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def slugify(text: str, *, fallback: str = "item", max_len: int = 64) -> str:
    value = re.sub(r"[^\w\s-]", "", (text or "").lower(), flags=re.UNICODE)
    value = re.sub(r"\s+", "_", value).strip("_")
    return (value[:max_len] or fallback).strip("_") or fallback


def json_default(value: Any) -> Any:
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, set):
        return sorted(value)
    raise TypeError(f"Object of type {type(value).__name__} is not JSON serializable")


@dataclass
class EntityType:
    entity_type_id: int
    entity_type: str
    entity_kind: str

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "EntityType":
        return cls(
            entity_type_id=int(data["entity_type_id"]),
            entity_type=str(data["entity_type"]),
            entity_kind=str(data["entity_kind"]),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "entity_type_id": self.entity_type_id,
            "entity_type": self.entity_type,
            "entity_kind": self.entity_kind,
        }


@dataclass
class EntityLocation:
    record_id: Optional[int] = None
    coords: Optional[dict[str, Any]] = None
    formatted_name: Optional[str] = None
    precision_level: Optional[int] = None
    geoid: Optional[str] = None
    level_1: Optional[str] = None
    level_1_id: Optional[str] = None
    level_2: Optional[str] = None
    level_2_id: Optional[str] = None
    level_3: Optional[str] = None
    level_3_id: Optional[str] = None
    level_4: Optional[str] = None
    level_4_id: Optional[str] = None
    level_5: Optional[str] = None
    level_5_id: Optional[str] = None
    level_6: Optional[str] = None
    level_6_id: Optional[str] = None
    level_7: Optional[str] = None
    level_7_id: Optional[str] = None

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "EntityLocation":
        fields = set(cls.__dataclass_fields__)
        return cls(**{k: v for k, v in data.items() if k in fields})

    def to_dict(self) -> dict[str, Any]:
        return dict(self.__dict__)


@dataclass
class Customer:
    entity_id: int
    name: str
    description: str = ""
    metadata: Optional[dict[str, Any]] = None
    keywords: Optional[list[Any]] = None
    filter_llm_prompt: Optional[str] = None
    added: Optional[str] = None
    modified: Optional[str] = None
    types: list[EntityType] = field(default_factory=list)
    locations: list[EntityLocation] = field(default_factory=list)
    aliases: list[str] = field(default_factory=list)
    related_entity_ids: list[int] = field(default_factory=list)

    @property
    def slug(self) -> str:
        return f"customer_{self.entity_id}"

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Customer":
        return cls(
            entity_id=int(data["entity_id"]),
            name=str(data["name"]),
            description=str(data.get("description") or ""),
            metadata=data.get("metadata"),
            keywords=data.get("keywords"),
            filter_llm_prompt=data.get("filter_llm_prompt"),
            added=data.get("added"),
            modified=data.get("modified"),
            types=[EntityType.from_dict(x) for x in data.get("types") or []],
            locations=[EntityLocation.from_dict(x) for x in data.get("locations") or []],
            aliases=[str(x) for x in data.get("aliases") or []],
            related_entity_ids=[int(x) for x in data.get("related_entity_ids") or []],
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "entity_id": self.entity_id,
            "name": self.name,
            "description": self.description,
            "metadata": self.metadata,
            "keywords": self.keywords,
            "filter_llm_prompt": self.filter_llm_prompt,
            "added": self.added,
            "modified": self.modified,
            "types": [x.to_dict() for x in self.types],
            "locations": [x.to_dict() for x in self.locations],
            "aliases": list(self.aliases),
            "related_entity_ids": list(self.related_entity_ids),
        }


@dataclass
class ContentGraph:
    customer: Customer
    event_supertypes: Optional[list[str]] = None
    theme_supertypes: Optional[list[str]] = None
    notes: str = ""

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ContentGraph":
        return cls(
            customer=Customer.from_dict(data["customer"]),
            event_supertypes=data.get("event_supertypes"),
            theme_supertypes=data.get("theme_supertypes"),
            notes=str(data.get("notes") or ""),
        )


@dataclass
class SourceItem:
    id: str
    kind: SourceKind
    text: str
    author: Optional[str] = None
    created_at: Optional[str] = None
    parent_source_id: Optional[str] = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def short_text(self, limit: int = 1200) -> str:
        text = (self.text or "").strip()
        return text if len(text) <= limit else text[:limit]

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "kind": self.kind,
            "text": self.text,
            "author": self.author,
            "created_at": self.created_at,
            "parent_source_id": self.parent_source_id,
            "metadata": dict(self.metadata),
        }


@dataclass
class ArticleBundle:
    source_id: str
    article: Optional[SourceItem] = None
    comments: list[SourceItem] = field(default_factory=list)
    posts: list[SourceItem] = field(default_factory=list)

    @property
    def items(self) -> list[SourceItem]:
        out: list[SourceItem] = []
        if self.article:
            out.append(self.article)
        out.extend(self.posts)
        out.extend(self.comments)
        return out


@dataclass
class SourceBatch:
    source_id: str
    extracted_records: list[dict[str, Any]]


@dataclass
class EventMention:
    """One extracted event record normalized just enough for linking."""

    source_id: str
    supertype: str
    event_type: str
    name: str = ""
    description: str = ""
    date_range: dict[str, Any] = field(default_factory=dict)
    location: dict[str, Any] = field(default_factory=dict)
    publication_date: Optional[str] = None
    raw: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_record(cls, record: dict[str, Any]) -> "EventMention":
        return cls(
            source_id=str(record.get("_source_id") or ""),
            supertype=str(record.get("_supertype") or ""),
            event_type=str(record.get("event_type") or ""),
            name=str(record.get("name") or ""),
            description=str(record.get("description") or ""),
            date_range=dict(record.get("date_range") or {}),
            location=dict(record.get("location") or {}),
            publication_date=record.get("date_created") or record.get("publication_date"),
            raw=dict(record),
        )

    @property
    def is_event(self) -> bool:
        return self.supertype.endswith("_event") and bool(self.event_type)

    @property
    def date_start(self) -> Optional[str]:
        return ((self.date_range.get("date_range") or {}).get("start")) or self.publication_date

    @property
    def date_end(self) -> Optional[str]:
        return ((self.date_range.get("date_range") or {}).get("end")) or self.date_start

    @property
    def level_2_id(self) -> str:
        geo = self.raw.get("_geo") or {}
        return str(
            geo.get("level_2_id")
            or self.location.get("state")
            or self.location.get("country")
            or ""
        )


@dataclass
class LinkedEvent:
    id: str
    event_type: str
    name: str = ""
    description: str = ""
    source_ids: list[str] = field(default_factory=list)
    date_range: dict[str, Any] = field(default_factory=dict)
    location: dict[str, Any] = field(default_factory=dict)
    publication_date: Optional[str] = None
    raw: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_mention(cls, event_id: str, mention: EventMention) -> "LinkedEvent":
        raw = dict(mention.raw)
        raw["id"] = event_id
        raw["source_ids"] = [mention.source_id] if mention.source_id else []
        raw["publication_date"] = mention.publication_date
        return cls(
            id=event_id,
            event_type=mention.event_type,
            name=mention.name,
            description=mention.description,
            source_ids=list(raw["source_ids"]),
            date_range=dict(mention.date_range),
            location=dict(mention.location),
            publication_date=mention.publication_date,
            raw=raw,
        )

    @property
    def date_start(self) -> Optional[str]:
        return ((self.date_range.get("date_range") or {}).get("start")) or self.publication_date

    @property
    def date_end(self) -> Optional[str]:
        return ((self.date_range.get("date_range") or {}).get("end")) or self.date_start

    @property
    def level_2_id(self) -> str:
        geo = self.raw.get("_geo") or {}
        return str(
            geo.get("level_2_id")
            or self.location.get("state")
            or self.location.get("country")
            or ""
        )

    def merge(self, mention: EventMention) -> None:
        if mention.source_id and mention.source_id not in self.source_ids:
            self.source_ids.append(mention.source_id)
        self.raw["source_ids"] = list(self.source_ids)
        for attr in ("name", "description"):
            if not getattr(self, attr) and getattr(mention, attr):
                setattr(self, attr, getattr(mention, attr))
                self.raw[attr] = getattr(mention, attr)
        if not self.location and mention.location:
            self.location = dict(mention.location)
            self.raw["location"] = dict(mention.location)
        if not self.date_range and mention.date_range:
            self.date_range = dict(mention.date_range)
            self.raw["date_range"] = dict(mention.date_range)
        if not self.publication_date or (
            mention.publication_date and mention.publication_date < self.publication_date
        ):
            self.publication_date = mention.publication_date
            self.raw["publication_date"] = mention.publication_date

    def to_record(self) -> dict[str, Any]:
        out = dict(self.raw)
        out.update(
            {
                "id": self.id,
                "event_type": self.event_type,
                "name": self.name,
                "description": self.description,
                "source_ids": list(self.source_ids),
                "date_range": self.date_range,
                "location": self.location,
                "publication_date": self.publication_date,
            }
        )
        return out


@dataclass
class LinkResult:
    status: LinkStatus
    event_id: Optional[str] = None
    event: Optional[LinkedEvent] = None
    reason: str = ""


@dataclass
class StanceEntry:
    id: str
    label: str
    description: str = ""
    created_at: str = field(default_factory=now_iso)
    aliases: list[str] = field(default_factory=list)

    @classmethod
    def new(cls, label: str, description: str = "", entry_id: Optional[str] = None) -> "StanceEntry":
        return cls(id=entry_id or slugify(label, fallback="stance"), label=label, description=description)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "label": self.label,
            "description": self.description,
            "created_at": self.created_at,
            "aliases": list(self.aliases),
        }


@dataclass
class StanceAssignment:
    source_item_id: str
    source_kind: SourceKind
    customer_id: int
    stance_id: str
    event_id: Optional[str] = None
    reason: str = ""
    assigned_at: str = field(default_factory=now_iso)

    def to_dict(self) -> dict[str, Any]:
        return dict(self.__dict__)


@dataclass
class StanceProposal:
    kind: Literal["add", "rename"]
    label: str
    description: str = ""
    source_item_ids: list[str] = field(default_factory=list)
    src_stance_id: Optional[str] = None


@dataclass
class StanceTagging:
    assignments: list[StanceAssignment] = field(default_factory=list)
    proposals: list[StanceProposal] = field(default_factory=list)
    dropped_assignments: int = 0


@dataclass
class StanceDecision:
    proposal_index: int
    action: Literal["accept", "reject", "rename", "generalise"]
    existing_id: Optional[str] = None
    new_label: Optional[str] = None
    new_description: Optional[str] = None
    reason: str = ""


@dataclass
class RawClaim:
    event_id: str
    customer_id: int
    affected_entity_ids: list[int]
    verbatim: str
    source_item_id: str
    source_kind: SourceKind
    importance: int = 1
    importance_reason: str = ""
    extracted_at: str = field(default_factory=now_iso)

    def to_dict(self) -> dict[str, Any]:
        return dict(self.__dict__)


@dataclass
class ClaimCluster:
    id: str
    customer_id: int
    event_id: str
    canonical: str
    members: list[RawClaim] = field(default_factory=list)
    created_at: str = field(default_factory=now_iso)
    is_new: bool = True
    aliases: list[str] = field(default_factory=list)

    @property
    def importance_max(self) -> int:
        return max((claim.importance for claim in self.members), default=0)

    @property
    def importance_typical(self) -> int:
        if not self.members:
            return 0
        return int(median(claim.importance for claim in self.members))

    @property
    def importance_n_high(self) -> int:
        return sum(1 for claim in self.members if claim.importance >= 3)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "customer_id": self.customer_id,
            "event_id": self.event_id,
            "canonical": self.canonical,
            "members": [x.to_dict() for x in self.members],
            "created_at": self.created_at,
            "is_new": self.is_new,
            "aliases": list(self.aliases),
            "importance_max": self.importance_max,
            "importance_typical": self.importance_typical,
            "importance_n_high": self.importance_n_high,
        }


@dataclass
class ClaimAssignment:
    source_item_id: str
    source_kind: SourceKind
    customer_id: int
    event_id: str
    cluster_id: str
    verbatim: str
    assigned_at: str = field(default_factory=now_iso)

    def to_dict(self) -> dict[str, Any]:
        return dict(self.__dict__)


@dataclass
class ClaimTagging:
    claims: list[RawClaim] = field(default_factory=list)
    dropped_off_customer: int = 0
    dropped_invalid: int = 0


@dataclass
class ClaimDecision:
    claim_index: int
    action: Literal["assign", "create", "drop"]
    cluster_id: Optional[str] = None
    canonical: Optional[str] = None
    reason: str = ""


@dataclass
class ClaimMutation:
    kind: Literal["rename", "merge"]
    cluster_id: Optional[str] = None
    new_canonical: Optional[str] = None
    src_id: Optional[str] = None
    dst_id: Optional[str] = None


@dataclass
class StepSummary:
    name: str
    counts: dict[str, int] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)

    def inc(self, key: str, amount: int = 1) -> None:
        self.counts[key] = self.counts.get(key, 0) + amount


@dataclass
class EventTagResult:
    event_id: str
    stance_tagging: StanceTagging
    stance_update: StepSummary
    claim_tagging: ClaimTagging
    claim_update: StepSummary


@dataclass
class ArticleProcessResult:
    source_id: str
    link_results: list[LinkResult] = field(default_factory=list)
    event_tag_results: list[EventTagResult] = field(default_factory=list)
    summaries: list[StepSummary] = field(default_factory=list)
