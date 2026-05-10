"""JSON snapshot read/write for the tags subsystem.

Two-key shape (per `data_model.md`):

```json
{
  "stance_catalog": {"customer_id": ..., "entries": [...], "assignments": [...]},
  "claim_catalogs": {"<customer_id>|<event_id>": {...}}
}
```
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from src.entities.tags.catalogs import ClaimCatalogStore, StanceCatalog
from src.entities.tags.models import json_default


def save_snapshot(
    stance_catalog: StanceCatalog,
    claim_catalogs: ClaimCatalogStore,
    path: Path,
) -> None:
    payload = {
        "stance_catalog": stance_catalog.to_dict(),
        "claim_catalogs": claim_catalogs.to_dict(),
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2, default=json_default)


def load_snapshot(path: Path) -> tuple[StanceCatalog, ClaimCatalogStore]:
    with open(path, encoding="utf-8") as f:
        payload: dict[str, Any] = json.load(f)
    stance = StanceCatalog.from_dict(payload["stance_catalog"])
    claims = ClaimCatalogStore.from_dict(payload.get("claim_catalogs") or {})
    return stance, claims


def load_stance_catalog(path: Path) -> StanceCatalog:
    """Bootstrap output is a single stance catalog (no claim catalogs)."""
    with open(path, encoding="utf-8") as f:
        payload = json.load(f)
    if "stance_catalog" in payload:
        return StanceCatalog.from_dict(payload["stance_catalog"])
    return StanceCatalog.from_dict(payload)


def save_stance_catalog(stance_catalog: StanceCatalog, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(
            {"stance_catalog": stance_catalog.to_dict()},
            f,
            ensure_ascii=False,
            indent=2,
            default=json_default,
        )
