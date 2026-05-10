"""Snapshot helpers for the in-memory tags_gpt state."""

from __future__ import annotations

import json
from pathlib import Path

from src.entities.tags_gpt.catalogs import ClaimCatalogStore, EventStore, StanceCatalog
from src.entities.tags_gpt.models import ContentGraph, json_default


def load_content_graph(path: Path) -> ContentGraph:
    with open(path, encoding="utf-8") as handle:
        return ContentGraph.from_dict(json.load(handle))


def save_snapshot(
    path: Path,
    *,
    event_store: EventStore,
    stance_catalog: StanceCatalog,
    claim_catalogs: ClaimCatalogStore,
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "events": event_store.to_records(),
        "stance_catalog": stance_catalog.to_dict(),
        "claim_catalogs": claim_catalogs.to_dict(),
    }
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2, default=json_default)
