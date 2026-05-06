"""Build a Stage-1 customer fixture for the tags subsystem.

Reads a single `kgdb.entities` row plus its joined `entity_types`,
`entity_locations`, `entities_alias`, and `relations` rows, and writes
the assembled JSON to `data/tags/customer_<entity_id>.json`.

This is a one-shot snapshot. Stage 2 will replace it with a live DB
read via `src.entities.tags.customer.load_customer_from_db`.

Usage:
    python scripts/build_customer_fixture.py 75
    python scripts/build_customer_fixture.py 75 --force      # overwrite

Required env vars (via .env.local or shell):
    KGDB_HOST, KGDB_PORT, KGDB_USER, KGDB_PASSWORD, KGDB_NAME
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import date, datetime
from pathlib import Path
from typing import Any

import psycopg2
import psycopg2.extras
from dotenv import load_dotenv


_PROJECT_ROOT = Path(__file__).resolve().parents[1]
load_dotenv(_PROJECT_ROOT / ".env.local")


_ENTITY_QUERY = """
    SELECT entity_id, name, description, metadata, keywords,
           filter_llm_prompt, added, modified
    FROM entities
    WHERE entity_id = %s
"""

_TYPES_QUERY = """
    SELECT etka.entity_type_id, etka.entity_type, etka.entity_kind
    FROM entity_types et
    JOIN entity_types_kinds_available etka
      ON etka.entity_type_id = et.entity_type_id
    WHERE et.entity_id = %s
"""

_LOCATIONS_QUERY = "SELECT * FROM entity_locations WHERE entity_id = %s"

_ALIASES_QUERY = (
    "SELECT entity_alias FROM entities_alias WHERE current_entity_id = %s"
)

_RELATIONS_QUERY = """
    SELECT ent_id_dest AS related_id FROM relations WHERE ent_id_source = %s
    UNION
    SELECT ent_id_source AS related_id FROM relations WHERE ent_id_dest = %s
"""


def _json_default(value: Any) -> Any:
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    raise TypeError(f"Object of type {type(value).__name__} is not JSON serializable")


def _connect():
    return psycopg2.connect(
        host=os.environ["KGDB_HOST"],
        port=int(os.environ.get("KGDB_PORT", 5432)),
        user=os.environ["KGDB_USER"],
        password=os.environ["KGDB_PASSWORD"],
        dbname=os.environ["KGDB_NAME"],
    )


def build_fixture(entity_id: int) -> dict:
    with _connect() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(_ENTITY_QUERY, (entity_id,))
            entity = cur.fetchone()
            if not entity:
                raise SystemExit(f"entity_id {entity_id} not found in kgdb.entities")

            cur.execute(_TYPES_QUERY, (entity_id,))
            types = [dict(r) for r in cur.fetchall()]

            cur.execute(_LOCATIONS_QUERY, (entity_id,))
            locations = [dict(r) for r in cur.fetchall()]

            cur.execute(_ALIASES_QUERY, (entity_id,))
            aliases = [r["entity_alias"] for r in cur.fetchall()]

            cur.execute(_RELATIONS_QUERY, (entity_id, entity_id))
            related_entity_ids = sorted({r["related_id"] for r in cur.fetchall()})

    customer = dict(entity)
    customer["types"] = types
    customer["locations"] = locations
    customer["aliases"] = aliases
    customer["related_entity_ids"] = related_entity_ids

    return {
        "customer": customer,
        "event_supertypes": None,
        "theme_supertypes": None,
        "notes": (
            f"Stage-1 fixture generated from kgdb entity_id={entity_id} via "
            "scripts/build_customer_fixture.py. Regenerate when the kgdb row "
            "changes; Stage 2 will replace this with a live DB read."
        ),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("entity_id", type=int, help="kgdb.entities.entity_id")
    parser.add_argument(
        "--force",
        action="store_true",
        help="Overwrite the fixture if it already exists.",
    )
    parser.add_argument(
        "--out-dir",
        type=Path,
        default=_PROJECT_ROOT / "data" / "tags",
        help="Directory the fixture is written to.",
    )
    args = parser.parse_args()

    out_path = args.out_dir / f"customer_{args.entity_id}.json"
    if out_path.exists() and not args.force:
        sys.exit(
            f"refusing to overwrite existing fixture {out_path} — "
            "pass --force to regenerate."
        )

    fixture = build_fixture(args.entity_id)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(fixture, f, ensure_ascii=False, indent=2, default=_json_default)
    print(f"wrote {out_path} (entity_id={args.entity_id})")


if __name__ == "__main__":
    main()
