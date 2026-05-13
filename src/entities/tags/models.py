"""Dataclass shapes for the tags subsystem.

See `data_model.md` for the field-level reference and `tags_design.md` for
how each shape participates in the pipelines.
"""

from __future__ import annotations

from dataclasses import dataclass, field, asdict
from datetime import date, datetime, timezone
from typing import Any, Literal, Optional


# ── Enums ───────────────────────────────────────────────────────────────

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

# Types that have their own entry-set in the catalog and can grow at
# streaming time. Consolidation operates on the same set.
STANCE_BEARING_TYPES: frozenset[StanceType] = frozenset(
    {
        "entity_stance",
        "complaint",
        "gratefulness",
        "suggestion",
        "request",
        "denuncia",
        "question",
        "endorsement",
    }
)

# Types that exist as assignment-only tags (no catalog entry; stance_id=None).
TAG_ONLY_TYPES: frozenset[StanceType] = frozenset({"noise"})

# Tie-break order — most specific first (see `tags_design.md` §3).
STANCE_TYPE_TIE_BREAK: tuple[StanceType, ...] = (
    "denuncia",
    "request",
    "complaint",
    "suggestion",
    "gratefulness",
    "endorsement",
    "entity_stance",
    "question",
    "noise",
)

SourceKind = Literal["article", "user_post", "user_comment"]
ImportanceHint = Literal["low", "medium", "high"]
StanceUpdateAction = Literal["accept", "reject", "rename", "generalise"]
ClaimUpdateAction = Literal["assign", "create", "drop"]
ClaimMutationKind = Literal["rename", "merge"]


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def json_default(value: Any) -> Any:
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, frozenset):
        return sorted(value)
    if isinstance(value, set):
        return sorted(value)
    return str(value)


# ── Source data ────────────────────────────────────────────────────────


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
        if not self.text or len(self.text) <= limit:
            return self.text or ""
        return self.text[: limit - 1] + "…"

    def to_dict(self) -> dict[str, Any]:
        return dict(self.__dict__)

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "SourceItem":
        return cls(
            id=str(payload["id"]),
            kind=payload.get("kind", "user_comment"),  # type: ignore[arg-type]
            text=payload.get("text", "") or "",
            author=payload.get("author"),
            created_at=payload.get("created_at"),
            parent_source_id=payload.get("parent_source_id"),
            metadata=dict(payload.get("metadata") or {}),
        )


@dataclass
class LinkedEventContext:
    """Compact event view used for prompt context and claim scoping."""

    id: str
    description: str
    event_type: Optional[str] = None
    name: Optional[str] = None

    def to_dict(self) -> dict[str, Any]:
        return dict(self.__dict__)

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "LinkedEventContext":
        return cls(
            id=str(payload["id"]),
            description=payload.get("description", "") or "",
            event_type=payload.get("event_type"),
            name=payload.get("name"),
        )


# ── Customer ────────────────────────────────────────────────────────────


@dataclass
class EntityType:
    entity_type_id: int
    entity_type: str
    entity_kind: str

    def to_dict(self) -> dict[str, Any]:
        return dict(self.__dict__)

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "EntityType":
        return cls(
            entity_type_id=int(payload["entity_type_id"]),
            entity_type=str(payload["entity_type"]),
            entity_kind=str(payload["entity_kind"]),
        )


@dataclass
class EntityLocation:
    """Mirrors `kgdb.entity_locations` columns. Optional fields kept loose.

    Stage-1 fixtures may not populate all columns; the prompt only ever
    needs `formatted_name` so we keep the rest as raw dicts to avoid
    schema churn.
    """

    coords: Optional[dict[str, Any]] = None
    formatted_name: Optional[str] = None
    precision_level: Optional[str] = None
    extra: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        out = {
            "coords": self.coords,
            "formatted_name": self.formatted_name,
            "precision_level": self.precision_level,
        }
        out.update(self.extra)
        return out

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "EntityLocation":
        known = {"coords", "formatted_name", "precision_level"}
        extra = {k: v for k, v in payload.items() if k not in known}
        return cls(
            coords=payload.get("coords"),
            formatted_name=payload.get("formatted_name"),
            precision_level=payload.get("precision_level"),
            extra=extra,
        )


@dataclass
class Customer:
    """Mirrors `kgdb.entities` + joined helpers.

    `to_dict` / `from_dict` are hand-written; consistency-pass state fields
    are persisted alongside the kgdb columns.
    """

    entity_id: int
    name: str
    description: str = ""
    metadata: Optional[dict[str, Any]] = None
    keywords: Optional[list[str]] = None
    filter_llm_prompt: Optional[str] = None
    added: str = ""
    modified: str = ""
    types: list[EntityType] = field(default_factory=list)
    locations: list[EntityLocation] = field(default_factory=list)
    aliases: list[str] = field(default_factory=list)
    related_entity_ids: list[int] = field(default_factory=list)

    # Consistency-pass state
    items_processed_total: int = 0
    items_processed_since_last_pass: int = 0
    # Bundle counters — one bundle = one root post/article + its comments.
    # Used by the consistency pass to size its windowed sample.
    bundles_processed_total: int = 0
    bundles_processed_since_last_pass: int = 0
    last_consistency_pass_at: Optional[str] = None
    last_consistency_pass_count: int = 0
    consistency_pass_threshold_items: int = 200
    consistency_pass_threshold_days: int = 7

    @property
    def slug(self) -> str:
        return f"customer_{self.entity_id}"

    def consistency_pass_due(self, now: datetime) -> bool:
        if self.items_processed_since_last_pass >= self.consistency_pass_threshold_items:
            return True
        if self.last_consistency_pass_at is None:
            return False
        last = datetime.fromisoformat(self.last_consistency_pass_at)
        if last.tzinfo is None:
            last = last.replace(tzinfo=timezone.utc)
        if now.tzinfo is None:
            now = now.replace(tzinfo=timezone.utc)
        return (now - last).days >= self.consistency_pass_threshold_days

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
            "types": [t.to_dict() for t in self.types],
            "locations": [loc.to_dict() for loc in self.locations],
            "aliases": list(self.aliases),
            "related_entity_ids": list(self.related_entity_ids),
            "items_processed_total": self.items_processed_total,
            "items_processed_since_last_pass": self.items_processed_since_last_pass,
            "bundles_processed_total": self.bundles_processed_total,
            "bundles_processed_since_last_pass": self.bundles_processed_since_last_pass,
            "last_consistency_pass_at": self.last_consistency_pass_at,
            "last_consistency_pass_count": self.last_consistency_pass_count,
            "consistency_pass_threshold_items": self.consistency_pass_threshold_items,
            "consistency_pass_threshold_days": self.consistency_pass_threshold_days,
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "Customer":
        # The Stage-1 fixture nests the kgdb row under "customer", with
        # event_supertypes / theme_supertypes / notes as siblings. Tolerate
        # both shapes.
        if "customer" in payload and isinstance(payload["customer"], dict):
            payload = payload["customer"]
        return cls(
            entity_id=int(payload["entity_id"]),
            name=str(payload["name"]),
            description=str(payload.get("description") or ""),
            metadata=payload.get("metadata"),
            keywords=payload.get("keywords"),
            filter_llm_prompt=payload.get("filter_llm_prompt"),
            added=str(payload.get("added") or ""),
            modified=str(payload.get("modified") or ""),
            types=[EntityType.from_dict(x) for x in (payload.get("types") or [])],
            locations=[EntityLocation.from_dict(x) for x in (payload.get("locations") or [])],
            aliases=list(payload.get("aliases") or []),
            related_entity_ids=list(payload.get("related_entity_ids") or []),
            items_processed_total=int(payload.get("items_processed_total") or 0),
            items_processed_since_last_pass=int(
                payload.get("items_processed_since_last_pass") or 0
            ),
            bundles_processed_total=int(payload.get("bundles_processed_total") or 0),
            bundles_processed_since_last_pass=int(
                payload.get("bundles_processed_since_last_pass") or 0
            ),
            last_consistency_pass_at=payload.get("last_consistency_pass_at"),
            last_consistency_pass_count=int(payload.get("last_consistency_pass_count") or 0),
            consistency_pass_threshold_items=int(
                payload.get("consistency_pass_threshold_items") or 200
            ),
            consistency_pass_threshold_days=int(
                payload.get("consistency_pass_threshold_days") or 7
            ),
        )


# ── Article bundle (the streaming unit) ─────────────────────────────────


@dataclass
class ArticleBundle:
    root: SourceItem
    comments: list[SourceItem] = field(default_factory=list)
    event_ids: list[str] = field(default_factory=list)
    linked_events: list[LinkedEventContext] = field(default_factory=list)
    customer: Optional[Customer] = None

    @property
    def all_items(self) -> list[SourceItem]:
        return [self.root, *self.comments]


# ── Stance ──────────────────────────────────────────────────────────────


@dataclass
class StanceEntry:
    id: str
    label: str
    description: str = ""
    primary_type: StanceType = "entity_stance"
    created_at: str = field(default_factory=now_iso)
    aliases: list[str] = field(default_factory=list)
    origin_event_id: Optional[str] = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "label": self.label,
            "description": self.description,
            "primary_type": self.primary_type,
            "created_at": self.created_at,
            "aliases": list(self.aliases),
            "origin_event_id": self.origin_event_id,
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "StanceEntry":
        return cls(
            id=str(payload["id"]),
            label=str(payload["label"]),
            description=str(payload.get("description") or ""),
            primary_type=payload.get("primary_type", "entity_stance"),  # type: ignore[arg-type]
            created_at=str(payload.get("created_at") or now_iso()),
            aliases=list(payload.get("aliases") or []),
            origin_event_id=payload.get("origin_event_id"),
        )


@dataclass
class StanceAssignment:
    source_item_id: str
    source_kind: SourceKind
    customer_id: int
    stance_id: Optional[str]
    stance_type: StanceType
    event_id: Optional[str] = None
    reason: str = ""
    assigned_at: str = field(default_factory=now_iso)

    def to_dict(self) -> dict[str, Any]:
        return dict(self.__dict__)

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "StanceAssignment":
        # Tolerate legacy snapshots that carry a `sentiment` field.
        return cls(
            source_item_id=str(payload["source_item_id"]),
            source_kind=payload.get("source_kind", "user_comment"),  # type: ignore[arg-type]
            customer_id=int(payload["customer_id"]),
            stance_id=payload.get("stance_id"),
            stance_type=payload.get("stance_type", "entity_stance"),  # type: ignore[arg-type]
            event_id=payload.get("event_id"),
            reason=str(payload.get("reason") or ""),
            assigned_at=str(payload.get("assigned_at") or now_iso()),
        )


@dataclass
class StanceProposal:
    kind: Literal["add", "rename"]
    label: str
    description: str = ""
    stance_type: StanceType = "entity_stance"
    source_item_ids: list[str] = field(default_factory=list)
    src_stance_id: Optional[str] = None

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
    action: StanceUpdateAction
    existing_id: Optional[str] = None
    new_label: Optional[str] = None
    new_description: Optional[str] = None
    reason: str = ""

    def to_dict(self) -> dict[str, Any]:
        return dict(self.__dict__)


# ── Claims ──────────────────────────────────────────────────────────────


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

    def to_dict(self) -> dict[str, Any]:
        return dict(self.__dict__)

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "RawClaim":
        return cls(
            event_id=str(payload["event_id"]),
            customer_id=int(payload["customer_id"]),
            verbatim=str(payload["verbatim"]),
            source_item_id=str(payload["source_item_id"]),
            source_kind=payload.get("source_kind", "article"),  # type: ignore[arg-type]
            importance=int(payload.get("importance") or 1),
            importance_reason=str(payload.get("importance_reason") or ""),
            extracted_at=str(payload.get("extracted_at") or now_iso()),
        )


@dataclass
class ClaimCluster:
    id: str
    customer_id: int
    event_id: str
    canonical: str
    members: list[RawClaim] = field(default_factory=list)
    aliases: list[str] = field(default_factory=list)
    created_at: str = field(default_factory=now_iso)
    is_new: bool = True
    freshness_window_hours: int = 24

    @property
    def importance_max(self) -> int:
        return max((m.importance for m in self.members), default=0)

    @property
    def importance_typical(self) -> int:
        if not self.members:
            return 0
        sorted_imp = sorted(m.importance for m in self.members)
        return sorted_imp[len(sorted_imp) // 2]

    @property
    def importance_n_high(self) -> int:
        return sum(1 for m in self.members if m.importance >= 3)

    def add_member(self, claim: RawClaim) -> None:
        self.members.append(claim)

    def rename(self, new_canonical: str) -> None:
        if self.canonical and self.canonical != new_canonical:
            self.aliases.append(self.canonical)
        self.canonical = new_canonical

    def freshness_expired(self, now: datetime) -> bool:
        try:
            created = datetime.fromisoformat(self.created_at)
        except ValueError:
            return False
        if created.tzinfo is None:
            created = created.replace(tzinfo=timezone.utc)
        if now.tzinfo is None:
            now = now.replace(tzinfo=timezone.utc)
        return (now - created).total_seconds() / 3600 >= self.freshness_window_hours

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "customer_id": self.customer_id,
            "event_id": self.event_id,
            "canonical": self.canonical,
            "members": [m.to_dict() for m in self.members],
            "aliases": list(self.aliases),
            "created_at": self.created_at,
            "is_new": self.is_new,
            "freshness_window_hours": self.freshness_window_hours,
            "importance_max": self.importance_max,
            "importance_typical": self.importance_typical,
            "importance_n_high": self.importance_n_high,
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "ClaimCluster":
        return cls(
            id=str(payload["id"]),
            customer_id=int(payload["customer_id"]),
            event_id=str(payload["event_id"]),
            canonical=str(payload["canonical"]),
            members=[RawClaim.from_dict(x) for x in (payload.get("members") or [])],
            aliases=list(payload.get("aliases") or []),
            created_at=str(payload.get("created_at") or now_iso()),
            is_new=bool(payload.get("is_new", True)),
            freshness_window_hours=int(payload.get("freshness_window_hours") or 24),
        )


@dataclass
class ClaimAssignment:
    source_item_id: str
    source_kind: SourceKind
    cluster_id: str
    event_id: str
    customer_id: int
    verbatim: str
    assigned_at: str = field(default_factory=now_iso)

    def to_dict(self) -> dict[str, Any]:
        return dict(self.__dict__)

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "ClaimAssignment":
        return cls(
            source_item_id=str(payload["source_item_id"]),
            source_kind=payload.get("source_kind", "article"),  # type: ignore[arg-type]
            cluster_id=str(payload["cluster_id"]),
            event_id=str(payload["event_id"]),
            customer_id=int(payload["customer_id"]),
            verbatim=str(payload.get("verbatim") or ""),
            assigned_at=str(payload.get("assigned_at") or now_iso()),
        )


@dataclass
class ClaimTagging:
    claims: list[RawClaim] = field(default_factory=list)
    dropped_invalid: int = 0
    dropped_off_customer: int = 0


@dataclass
class ClaimDecision:
    claim_index: int
    action: ClaimUpdateAction
    cluster_id: Optional[str] = None
    canonical: Optional[str] = None
    reason: str = ""


@dataclass
class ClaimMutation:
    kind: ClaimMutationKind
    cluster_id: Optional[str] = None
    new_canonical: Optional[str] = None
    src_id: Optional[str] = None
    dst_id: Optional[str] = None


# ── Triage ──────────────────────────────────────────────────────────────


@dataclass
class TypeTriageItem:
    """One row per distinct stance idea (post tie-break)."""

    source_item_id: str
    source_kind: SourceKind
    stance_type: StanceType
    brief_summary: str = ""
    importance_hint: Optional[ImportanceHint] = None
    text: str = ""

    def to_dict(self) -> dict[str, Any]:
        return dict(self.__dict__)


@dataclass
class TypeTriageResult:
    triaged: list[TypeTriageItem] = field(default_factory=list)
    n_items_seen: int = 0


# ── Step results ────────────────────────────────────────────────────────


@dataclass
class StepSummary:
    name: str
    counters: dict[str, int] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)

    def inc(self, key: str, n: int = 1) -> None:
        self.counters[key] = self.counters.get(key, 0) + n

    def note(self, message: str) -> None:
        self.notes.append(message)

    def to_dict(self) -> dict[str, Any]:
        return {"name": self.name, "counters": dict(self.counters), "notes": list(self.notes)}


@dataclass
class EventTagResult:
    event_id: str
    stance_tagging: Optional[StanceTagging] = None
    stance_update: Optional[StepSummary] = None
    claim_tagging: Optional[ClaimTagging] = None
    claim_update: Optional[StepSummary] = None


@dataclass
class ArticleProcessResult:
    source_id: str
    summaries: list[StepSummary] = field(default_factory=list)
    event_tag_results: list[EventTagResult] = field(default_factory=list)


@dataclass
class ConsistencyPassResult:
    customer_id: int
    started_at: str = field(default_factory=now_iso)
    finished_at: str = ""
    proposals: list[StanceProposal] = field(default_factory=list)
    merge_pairs: list[tuple[str, str]] = field(default_factory=list)
    retire_ids: list[str] = field(default_factory=list)
    decisions: list[StanceDecision] = field(default_factory=list)
    summary: Optional[StepSummary] = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "customer_id": self.customer_id,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "proposals": [p.to_dict() for p in self.proposals],
            "merge_pairs": [list(p) for p in self.merge_pairs],
            "retire_ids": list(self.retire_ids),
            "decisions": [d.to_dict() for d in self.decisions],
            "summary": self.summary.to_dict() if self.summary else None,
        }
