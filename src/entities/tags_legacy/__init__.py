"""Tags subsystem — customer-anchored stances + event-scoped claim clusters.

See `tags_overview.md` for the design and `tags_impl_plan.md` for the
class spec / lifecycle. Stage 1 is in-memory only — no DB writes.

Data models live in `src.entities.tags.models`; the LLM-driven phases
and infrastructure (retrieval, persistence, stats) live alongside this
file.
"""

from src.entities.tags.models import (
    ClaimAssignment,
    ClaimCatalog,
    ClaimCatalogRegistry,
    ClaimCluster,
    ContentGraphConfig,
    Customer,
    EntityLocation,
    EntityType,
    RawClaim,
    SourceItem,
    StanceAssignment,
    StanceCatalog,
    StanceEntry,
    load_customer_from_db,
    load_customer_from_json,
)
from src.entities.tags.persistence import InMemoryPersistence, Persistence

__all__ = [
    "ClaimAssignment",
    "ClaimCatalog",
    "ClaimCatalogRegistry",
    "ClaimCluster",
    "ContentGraphConfig",
    "Customer",
    "EntityLocation",
    "EntityType",
    "InMemoryPersistence",
    "load_customer_from_db",
    "load_customer_from_json",
    "Persistence",
    "RawClaim",
    "SourceItem",
    "StanceAssignment",
    "StanceCatalog",
    "StanceEntry",
]
