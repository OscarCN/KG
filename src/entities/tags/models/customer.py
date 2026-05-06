"""Customer dataclass + content-graph configuration.

The dataclass shape mirrors the kgdb `entities` columns plus the joined
helper tables (`entity_types_kinds_available`, `entity_locations`,
`entities_alias`, `relations`) so that the Stage-1 JSON fixture is
structurally identical to a Stage-2 live DB read. The Stage-2 swap
replaces `load_customer_from_json` with `load_customer_from_db`.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional


@dataclass
class EntityType:
    entity_type_id: int
    entity_type: str
    entity_kind: str

    @classmethod
    def from_dict(cls, d: dict) -> "EntityType":
        return cls(
            entity_type_id=int(d["entity_type_id"]),
            entity_type=d["entity_type"],
            entity_kind=d["entity_kind"],
        )


@dataclass
class EntityLocation:
    """Mirrors `kgdb.entity_locations` columns. All non-`entity_id` fields
    optional so a row missing a location can still be represented."""

    record_id: Optional[int] = None
    coords: Optional[dict] = None
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
    def from_dict(cls, d: dict) -> "EntityLocation":
        known = {f.name for f in cls.__dataclass_fields__.values()}
        return cls(**{k: v for k, v in d.items() if k in known})


@dataclass
class Customer:
    entity_id: int
    name: str
    description: str
    metadata: Optional[dict] = None
    keywords: Optional[list] = None
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
    def from_kgdb_row(
        cls,
        row: dict,
        types: list[dict],
        locations: list[dict],
        aliases: list[str],
        related_entity_ids: list[int],
    ) -> "Customer":
        return cls(
            entity_id=int(row["entity_id"]),
            name=row["name"],
            description=row["description"],
            metadata=row.get("metadata"),
            keywords=row.get("keywords"),
            filter_llm_prompt=row.get("filter_llm_prompt"),
            added=str(row["added"]) if row.get("added") else None,
            modified=str(row["modified"]) if row.get("modified") else None,
            types=[EntityType.from_dict(t) for t in types],
            locations=[EntityLocation.from_dict(loc) for loc in locations],
            aliases=list(aliases),
            related_entity_ids=list(related_entity_ids),
        )

    @classmethod
    def from_dict(cls, d: dict) -> "Customer":
        return cls(
            entity_id=int(d["entity_id"]),
            name=d["name"],
            description=d["description"],
            metadata=d.get("metadata"),
            keywords=d.get("keywords"),
            filter_llm_prompt=d.get("filter_llm_prompt"),
            added=d.get("added"),
            modified=d.get("modified"),
            types=[EntityType.from_dict(t) for t in (d.get("types") or [])],
            locations=[EntityLocation.from_dict(loc) for loc in (d.get("locations") or [])],
            aliases=list(d.get("aliases") or []),
            related_entity_ids=[int(x) for x in (d.get("related_entity_ids") or [])],
        )


@dataclass
class ContentGraphConfig:
    customer: Customer
    event_supertypes: Optional[list[str]] = None
    theme_supertypes: Optional[list[str]] = None
    notes: str = ""

    @classmethod
    def from_dict(cls, d: dict) -> "ContentGraphConfig":
        return cls(
            customer=Customer.from_dict(d["customer"]),
            event_supertypes=d.get("event_supertypes"),
            theme_supertypes=d.get("theme_supertypes"),
            notes=d.get("notes", ""),
        )


def load_customer_from_json(path: Path) -> ContentGraphConfig:
    """Stage-1 entry point — read a JSON fixture produced by
    `scripts/build_customer_fixture.py`."""
    with open(path, encoding="utf-8") as f:
        raw = json.load(f)
    return ContentGraphConfig.from_dict(raw)


def load_customer_from_db(entity_id: int, conn: Any) -> ContentGraphConfig:
    """Stage-2 entry point — placeholder. Implementation lands when
    Postgres reads are wired into the runtime; until then, callers go
    through `load_customer_from_json` against a fixture produced by
    `scripts/build_customer_fixture.py`."""
    raise NotImplementedError(
        "Stage 2 — live DB reads not wired yet. See tags_overview.md."
    )
