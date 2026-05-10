"""Persistence interface + Stage-1 in-memory implementation.

Stage 2 will add a Postgres-backed implementation that satisfies the
same Protocol — no consumer-side changes needed.
"""

from __future__ import annotations

import json
from datetime import date, datetime
from pathlib import Path
from typing import Any, Optional, Protocol

from src.entities.tags.models.claim_catalog import ClaimCatalogRegistry
from src.entities.tags.models.stance_catalog import StanceCatalog


def _json_default(value: Any) -> Any:
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, set):
        return sorted(value)
    raise TypeError(f"Object of type {type(value).__name__} is not JSON serializable")


class Persistence(Protocol):
    def save_snapshot(
        self,
        stance_catalog: StanceCatalog,
        claim_catalogs: ClaimCatalogRegistry,
        out_path: Path,
    ) -> None: ...

    def load_stance_catalog(self, path: Path) -> Optional[StanceCatalog]: ...


class InMemoryPersistence:
    """Stage-1 implementation. Catalogs live in RAM during the run; the
    only durable artefact is an on-demand snapshot dump (used by the
    streaming runner at exit for inspection)."""

    def save_snapshot(
        self,
        stance_catalog: StanceCatalog,
        claim_catalogs: ClaimCatalogRegistry,
        out_path: Path,
    ) -> None:
        out_path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "stance_catalog": stance_catalog.to_dict(),
            "claim_catalogs": claim_catalogs.to_dict(),
        }
        with open(out_path, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2, default=_json_default)

    def load_stance_catalog(self, path: Path) -> Optional[StanceCatalog]:
        if not path.exists():
            return None
        with open(path, encoding="utf-8") as f:
            payload = json.load(f)
        return StanceCatalog.from_dict(payload["stance_catalog"])
