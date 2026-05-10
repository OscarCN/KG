"""JSON persistence helpers for tags_gpt snapshots."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from src.entities.tags_gpt.catalogs import ClaimCatalogStore, StanceCatalog
from src.entities.tags_gpt.models import Customer, json_default


def load_customer(path: Path) -> Customer:
    with open(path, encoding="utf-8") as handle:
        raw = json.load(handle)
    return Customer.from_dict(raw.get("customer", raw))


def save_snapshot(path: Path, *, stance_catalog: StanceCatalog, claim_catalogs: ClaimCatalogStore) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "stance_catalog": stance_catalog.to_dict(),
        "claim_catalogs": claim_catalogs.to_dict(),
    }
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2, default=json_default)


def load_snapshot(path: Path) -> tuple[StanceCatalog, ClaimCatalogStore]:
    with open(path, encoding="utf-8") as handle:
        raw: dict[str, Any] = json.load(handle)
    return (
        StanceCatalog.from_dict(raw.get("stance_catalog") or {}),
        ClaimCatalogStore.from_dict(raw.get("claim_catalogs") or {}),
    )


def save_bootstrap(path: Path, stance_catalog: StanceCatalog) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        json.dump({"stance_catalog": stance_catalog.to_dict()}, handle, ensure_ascii=False, indent=2)


def load_bootstrap(path: Path) -> StanceCatalog:
    with open(path, encoding="utf-8") as handle:
        raw = json.load(handle)
    return StanceCatalog.from_dict(raw.get("stance_catalog", raw))

