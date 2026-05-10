"""Pure data models for the tags_gpt pipeline."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from statistics import median
from typing import Any, Literal


StanceType = Literal[
    "entity_stance",
    "complaint",
    "gratefulness",
    "suggestion",
    "request",
    "denuncia",
    "question",
    "endorsement",
    "noise",
]

STANCE_TYPES: tuple[StanceType, ...] = (
    "entity_stance",
    "complaint",
    "gratefulness",
    "suggestion",
    "request",
    "denuncia",
    "question",
    "endorsement",
    "noise",
)
STANCE_BEARING_TYPES: set[StanceType] = {
    "entity_stance",
    "complaint",
    "gratefulness",
    "suggestion",
    "request",
    "denuncia",
    "question",
    "endorsement",
}
TAG_ONLY_TYPES: set[StanceType] = {"noise"}

SourceKind = Literal["article", "user_post", "user_comment"]
ImportanceHint = Literal["low", "medium", "high"]


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def json_default(value: Any) -> Any:
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, set):
        return sorted(value)
    raise TypeError(f"Object of type {type(value).__name__} is not JSON serializable")


def slugify(text: str, *, fallback: str = "item", max_len: int = 64) -> str:
    value = re.sub(r"[^\w\s-]", "", (text or "").lower(), flags=re.UNICODE)
    value = re.sub(r"\s+", "_", value).strip("_")
    return (value[:max_len] or fallback).strip("_") or fallback


def _filtered_dataclass_kwargs(cls, data: dict[str, Any]) -> dict[str, Any]:
    fields = set(cls.__dataclass_fields__)  # type: ignore[attr-defined]
    return {key: value for key, value in data.items() if key in fields}


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
        return dict(self.__dict__)


@dataclass
class EntityLocation:
    record_id: int | None = None
    coords: dict[str, Any] | None = None
    formatted_name: str | None = None
    precision_level: int | None = None
    geoid: str | None = None
    level_1: str | None = None
    level_1_id: str | None = None
    level_2: str | None = None
    level_2_id: str | None = None
    level_3: str | None = None
    level_3_id: str | None = None
    level_4: str | None = None
    level_4_id: str | None = None
    level_5: str | None = None
    level_5_id: str | None = None
    level_6: str | None = None
    level_6_id: str | None = None
    level_7: str | None = None
    level_7_id: str | None = None

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "EntityLocation":
        return cls(**_filtered_dataclass_kwargs(cls, data))

    def to_dict(self) -> dict[str, Any]:
        return dict(self.__dict__)


@dataclass
class Customer:
    entity_id: int
    name: str
    description: str = ""
    metadata: dict[str, Any] | None = None
    keywords: list[str] | None = None
    filter_llm_prompt: str | None = None
    added: str | None = None
    modified: str | None = None
    types: list[EntityType] = field(default_factory=list)
    locations: list[EntityLocation] = field(default_factory=list)
    aliases: list[str] = field(default_factory=list)
    related_entity_ids: list[int] = field(default_factory=list)
    items_processed_total: int = 0
    items_processed_since_last_pass: int = 0
    last_consistency_pass_at: str | None = None
    last_consistency_pass_count: int = 0
    consistency_pass_threshold_items: int = 200
    consistency_pass_threshold_days: int = 7

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
            items_processed_total=int(data.get("items_processed_total") or 0),
            items_processed_since_last_pass=int(data.get("items_processed_since_last_pass") or 0),
            last_consistency_pass_at=data.get("last_consistency_pass_at"),
            last_consistency_pass_count=int(data.get("last_consistency_pass_count") or 0),
            consistency_pass_threshold_items=int(data.get("consistency_pass_threshold_items") or 200),
            consistency_pass_threshold_days=int(data.get("consistency_pass_threshold_days") or 7),
        )

    def consistency_pass_due(self, now: datetime | None = None) -> bool:
        now = now or datetime.now(timezone.utc)
        if self.items_processed_since_last_pass >= self.consistency_pass_threshold_items:
            return True
        if not self.last_consistency_pass_at:
            return self.items_processed_total > 0
        try:
            last = datetime.fromisoformat(self.last_consistency_pass_at)
        except ValueError:
            return True
        return now - last >= timedelta(days=self.consistency_pass_threshold_days)

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
            "items_processed_total": self.items_processed_total,
            "items_processed_since_last_pass": self.items_processed_since_last_pass,
            "last_consistency_pass_at": self.last_consistency_pass_at,
            "last_consistency_pass_count": self.last_consistency_pass_count,
            "consistency_pass_threshold_items": self.consistency_pass_threshold_items,
            "consistency_pass_threshold_days": self.consistency_pass_threshold_days,
        }


@dataclass
class SourceItem:
    id: str
    kind: SourceKind
    text: str
    author: str | None = None
    created_at: str | None = None
    parent_source_id: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "SourceItem":
        return cls(
            id=str(data["id"]),
            kind=data.get("kind", "article"),
            text=str(data.get("text") or ""),
            author=data.get("author"),
            created_at=data.get("created_at"),
            parent_source_id=data.get("parent_source_id"),
            metadata=dict(data.get("metadata") or {}),
        )

    def short_text(self, limit: int = 1200) -> str:
        text = (self.text or "").strip()
        return text if len(text) <= limit else text[:limit]

    def to_dict(self) -> dict[str, Any]:
        return dict(self.__dict__)


@dataclass
class LinkedEventContext:
    id: str
    description: str

    @classmethod
    def from_dict(cls, event_id: str, data: dict[str, Any] | str) -> "LinkedEventContext":
        if isinstance(data, str):
            return cls(id=str(event_id), description=data)
        return cls(id=str(data.get("id") or event_id), description=str(data.get("description") or ""))

    def to_dict(self) -> dict[str, Any]:
        return {"id": self.id, "description": self.description}


@dataclass
class ArticleBundle:
    root: SourceItem
    comments: list[SourceItem] = field(default_factory=list)
    event_ids: list[str] = field(default_factory=list)
    linked_events: list[LinkedEventContext] = field(default_factory=list)
    customer: Customer | None = None

    @property
    def source_id(self) -> str:
        return self.root.id

    @property
    def items(self) -> list[SourceItem]:
        return [self.root, *self.comments]


@dataclass
class StanceEntry:
    id: str
    label: str
    description: str
    primary_type: StanceType
    created_at: str = field(default_factory=now_iso)
    aliases: list[str] = field(default_factory=list)
    origin_event_id: str | None = None
    retired_at: str | None = None

    @classmethod
    def new(
        cls,
        label: str,
        description: str,
        *,
        entry_id: str | None = None,
        primary_type: StanceType,
        origin_event_id: str | None = None,
    ) -> "StanceEntry":
        return cls(
            id=entry_id or slugify(label, fallback="stance"),
            label=label,
            description=description,
            primary_type=primary_type,
            origin_event_id=origin_event_id,
        )

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "StanceEntry":
        return cls(**_filtered_dataclass_kwargs(cls, data))

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "label": self.label,
            "description": self.description,
            "primary_type": self.primary_type,
            "created_at": self.created_at,
            "aliases": list(self.aliases),
            "origin_event_id": self.origin_event_id,
            "retired_at": self.retired_at,
        }


@dataclass
class StanceAssignment:
    source_item_id: str
    source_kind: SourceKind
    customer_id: int
    stance_id: str | None
    stance_type: StanceType
    event_id: str | None = None
    reason: str = ""
    assigned_at: str = field(default_factory=now_iso)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "StanceAssignment":
        return cls(**_filtered_dataclass_kwargs(cls, data))

    def to_dict(self) -> dict[str, Any]:
        return dict(self.__dict__)


@dataclass
class StanceProposal:
    kind: Literal["add", "rename"]
    label: str
    description: str
    stance_type: StanceType
    source_item_ids: list[str] = field(default_factory=list)
    src_stance_id: str | None = None

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "StanceProposal":
        return cls(**_filtered_dataclass_kwargs(cls, data))

    def to_dict(self) -> dict[str, Any]:
        return dict(self.__dict__)


@dataclass
class StanceTagging:
    assignments: list[StanceAssignment] = field(default_factory=list)
    proposals: list[StanceProposal] = field(default_factory=list)
    dropped_assignments: int = 0
    n_assignments_by_type: dict[str, int] = field(default_factory=dict)
    n_items_tagged_no_stance: int = 0


@dataclass
class StanceDecision:
    proposal_index: int
    action: Literal["accept", "reject", "rename", "generalise"]
    existing_id: str | None = None
    new_label: str | None = None
    new_description: str | None = None
    reason: str = ""

    def to_dict(self) -> dict[str, Any]:
        return dict(self.__dict__)


@dataclass
class RawClaim:
    event_id: str
    customer_id: int
    verbatim: str
    source_item_id: str
    source_kind: SourceKind
    importance: int = 1
    importance_reason: str = ""
    extracted_at: str = field(default_factory=now_iso)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "RawClaim":
        return cls(**_filtered_dataclass_kwargs(cls, data))

    def to_dict(self) -> dict[str, Any]:
        return dict(self.__dict__)


@dataclass
class ClaimCluster:
    id: str
    customer_id: int
    event_id: str
    canonical: str
    members: list[RawClaim] = field(default_factory=list)
    aliases: list[str] = field(default_factory=list)
    created_at: str = field(default_factory=now_iso)
    importance_max: int = 1
    importance_typical: int = 1
    importance_n_high: int = 0
    is_new: bool = True
    freshness_window_hours: int = 24

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ClaimCluster":
        payload = dict(data)
        payload["members"] = [RawClaim.from_dict(x) for x in payload.get("members") or []]
        return cls(**_filtered_dataclass_kwargs(cls, payload))

    def recompute_importance(self) -> None:
        values = [max(1, min(3, int(claim.importance))) for claim in self.members] or [1]
        self.importance_max = max(values)
        self.importance_typical = int(median(values))
        self.importance_n_high = sum(1 for value in values if value == 3)

    def to_dict(self) -> dict[str, Any]:
        self.recompute_importance()
        return {
            "id": self.id,
            "customer_id": self.customer_id,
            "event_id": self.event_id,
            "canonical": self.canonical,
            "members": [x.to_dict() for x in self.members],
            "aliases": list(self.aliases),
            "created_at": self.created_at,
            "importance_max": self.importance_max,
            "importance_typical": self.importance_typical,
            "importance_n_high": self.importance_n_high,
            "is_new": self.is_new,
            "freshness_window_hours": self.freshness_window_hours,
        }


@dataclass
class ClaimAssignment:
    source_item_id: str
    source_kind: SourceKind
    cluster_id: str
    event_id: str
    customer_id: int
    verbatim: str
    assigned_at: str = field(default_factory=now_iso)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ClaimAssignment":
        return cls(**_filtered_dataclass_kwargs(cls, data))

    def to_dict(self) -> dict[str, Any]:
        return dict(self.__dict__)


@dataclass
class ClaimTagging:
    claims: list[RawClaim] = field(default_factory=list)
    dropped_invalid: int = 0
    dropped_off_customer: int = 0


@dataclass
class ClaimDecision:
    claim_index: int
    action: Literal["assign", "create", "drop"]
    cluster_id: str | None = None
    canonical: str | None = None
    reason: str = ""


@dataclass
class ClaimMutation:
    kind: Literal["rename", "merge"]
    cluster_id: str | None = None
    new_canonical: str | None = None
    src_id: str | None = None
    dst_id: str | None = None


@dataclass
class TypeTriageItem:
    source_item_id: str
    source_kind: SourceKind
    stance_type: StanceType
    brief_summary: str
    importance_hint: ImportanceHint | None = None


@dataclass
class TypeTriageResult:
    triaged: list[TypeTriageItem] = field(default_factory=list)
    n_items_seen: int = 0
    dropped_invalid: int = 0


@dataclass
class StepSummary:
    name: str
    counters: dict[str, int] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)

    def inc(self, key: str, n: int = 1) -> None:
        self.counters[key] = self.counters.get(key, 0) + n

    def to_dict(self) -> dict[str, Any]:
        return {"name": self.name, "counters": dict(self.counters), "notes": list(self.notes)}


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
    summaries: list[StepSummary] = field(default_factory=list)
    event_tag_results: list[EventTagResult] = field(default_factory=list)


@dataclass
class ConsistencyPassResult:
    customer_id: int
    started_at: str
    finished_at: str
    sample_size: int
    sample_strategy: dict[str, Any]
    proposals: list[StanceProposal] = field(default_factory=list)
    merge_pairs: list[tuple[str, str]] = field(default_factory=list)
    retire_ids: list[str] = field(default_factory=list)
    reroute_pairs: list[tuple[str, str]] = field(default_factory=list)
    decisions: list[StanceDecision] = field(default_factory=list)
    summary: StepSummary = field(default_factory=lambda: StepSummary("consistency_pass"))

