"""Data models for the tags subsystem.

Pure data structures — no LLM calls, no IO. Stage 2 will swap the
persistence layer below them without changing these classes.
"""

from src.entities.tags.models.claim_catalog import (
    ClaimAssignment,
    ClaimCatalog,
    ClaimCatalogRegistry,
    ClaimCluster,
    RawClaim,
)
from src.entities.tags.models.customer import (
    ContentGraphConfig,
    Customer,
    EntityLocation,
    EntityType,
    load_customer_from_db,
    load_customer_from_json,
)
from src.entities.tags.models.source_item import SourceItem, SourceKind
from src.entities.tags.models.stance_catalog import (
    StanceAssignment,
    StanceCatalog,
    StanceEntry,
)

__all__ = [
    "ClaimAssignment",
    "ClaimCatalog",
    "ClaimCatalogRegistry",
    "ClaimCluster",
    "RawClaim",
    "ContentGraphConfig",
    "Customer",
    "EntityLocation",
    "EntityType",
    "load_customer_from_db",
    "load_customer_from_json",
    "SourceItem",
    "SourceKind",
    "StanceAssignment",
    "StanceCatalog",
    "StanceEntry",
]
