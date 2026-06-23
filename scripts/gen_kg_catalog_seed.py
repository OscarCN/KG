"""Generate the kgdb KG type-catalog seed SQL (P2 of kgdb event persistence).

Reads the entity-extraction schemas (`src/entities/extraction/schemas/*.json`)
and the leaf-type mapping (`catalogues/event_types.csv`), and emits idempotent
`INSERT ... ON CONFLICT` statements that seed `entity_types_kinds_available`:

  * one **supertype** row per event/entity schema — `entity_kind` from
    `meta.category`, `metadata_template` = the whole schema JSON, `parent_entity_type`
    NULL.
  * one **child** row per leaf `event_type` — `parent_entity_type` -> its supertype
    (resolved by subquery), `metadata_template` NULL (children inherit the schema).

Themes (`meta.category == "theme"`) are skipped: `entity_kinds_available` has no
`theme` kind yet (see `docs/todos/kgdb_event_persistence.md`).

Upsert key is the live `UNIQUE (entity_type, entity_kind)` constraint, so re-running
the generated SQL refreshes descriptions / schema JSON without duplicating rows.

Output: `media-backend-paid/docs/kg_catalog_seed_kgdb.sql`
Apply (per `dev/docs/db/runbook.md`) AFTER `db/kg_db/inserts_catalog_tables.sql`.

Usage:
    python scripts/gen_kg_catalog_seed.py            # write the seed file
    python scripts/gen_kg_catalog_seed.py --stdout   # print instead of writing
"""

from __future__ import annotations

import argparse
import csv
import json
from collections import defaultdict
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parents[1]
_SCHEMAS_DIR = _PROJECT_ROOT / "src" / "entities" / "extraction" / "schemas"
_EVENT_TYPES_CSV = (
    _PROJECT_ROOT / "src" / "entities" / "extraction" / "catalogues" / "event_types.csv"
)
_OUT_PATH = (
    _PROJECT_ROOT.parent.parent / "media-backend-paid" / "docs" / "kg_catalog_seed_kgdb.sql"
).resolve()

# meta.category -> kgdb.entity_kinds_available.kind. "theme" is intentionally absent.
_CATEGORY_TO_KIND = {"event": "event", "entity": "entity"}

_DOLLAR_TAG = "$tmpl$"


def _sql_text(value: str) -> str:
    """Single-quoted SQL string literal (doubles embedded quotes)."""
    return "'" + value.replace("'", "''") + "'"


def _sql_json(raw_json_text: str) -> str:
    """Dollar-quoted JSON literal cast to ``json`` (no escaping of quotes/backslashes)."""
    if _DOLLAR_TAG in raw_json_text:
        raise ValueError(f"schema JSON contains the dollar-quote tag {_DOLLAR_TAG!r}")
    return f"{_DOLLAR_TAG}{raw_json_text}{_DOLLAR_TAG}::json"


def _load_supertypes() -> dict[str, dict]:
    """supertype name -> {kind, description, metadata_template_sql}. Themes skipped."""
    supertypes: dict[str, dict] = {}
    for path in sorted(_SCHEMAS_DIR.glob("*.json")):
        supertype = path.stem
        raw = path.read_text(encoding="utf-8")
        obj = json.loads(raw)
        root = next(iter(obj))  # PascalCase class key
        meta = obj[root].get("meta", {})
        category = meta.get("category")
        kind = _CATEGORY_TO_KIND.get(category)
        if kind is None:
            continue  # theme (or unknown category) — not seeded yet
        description = (meta.get("description") or supertype).strip()
        supertypes[supertype] = {
            "kind": kind,
            "description": description,
            "metadata_template_sql": _sql_json(raw),
        }
    return supertypes


def _load_leaves() -> dict[str, list[tuple[str, str]]]:
    """supertype -> [(event_type, label_es), ...] from event_types.csv."""
    leaves: dict[str, list[tuple[str, str]]] = defaultdict(list)
    seen: dict[str, str] = {}
    with open(_EVENT_TYPES_CSV, encoding="utf-8") as f:
        for row in csv.DictReader(f):
            event_type = (row["event_type"] or "").strip()
            supertype = (row["supertype"] or "").strip()
            label_es = (row.get("label_es") or "").strip()
            if not event_type or not supertype:
                continue
            if event_type in seen and seen[event_type] != supertype:
                raise ValueError(
                    f"leaf {event_type!r} maps to both {seen[event_type]!r} and "
                    f"{supertype!r} — leaf names must be unique (entity_type, entity_kind)"
                )
            seen[event_type] = supertype
            leaves[supertype].append((event_type, label_es or event_type))
    return leaves


def _supertype_block(supertype: str, info: dict, leaves: list[tuple[str, str]]) -> str:
    kind = info["kind"]
    parent_lookup = (
        f"(SELECT entity_type_id FROM public.entity_types_kinds_available "
        f"WHERE entity_type = {_sql_text(supertype)} AND entity_kind = {_sql_text(kind)})"
    )
    lines = [f"-- {supertype} ({kind}) + {len(leaves)} child type(s)"]
    # Supertype row (carries the schema JSON).
    lines.append(
        "INSERT INTO public.entity_types_kinds_available "
        "(entity_type, entity_kind, description, parent_entity_type, metadata_template)\n"
        f"VALUES ({_sql_text(supertype)}, {_sql_text(kind)}, "
        f"{_sql_text(info['description'])}, NULL, {info['metadata_template_sql']})\n"
        "ON CONFLICT (entity_type, entity_kind) DO UPDATE SET\n"
        "    description = EXCLUDED.description,\n"
        "    metadata_template = EXCLUDED.metadata_template,\n"
        "    parent_entity_type = EXCLUDED.parent_entity_type;"
    )
    # Child rows (inherit the schema -> metadata_template NULL).
    for event_type, label in leaves:
        lines.append(
            "INSERT INTO public.entity_types_kinds_available "
            "(entity_type, entity_kind, description, parent_entity_type, metadata_template)\n"
            f"VALUES ({_sql_text(event_type)}, {_sql_text(kind)}, "
            f"{_sql_text(label)}, {parent_lookup}, NULL)\n"
            "ON CONFLICT (entity_type, entity_kind) DO UPDATE SET\n"
            "    description = EXCLUDED.description,\n"
            "    parent_entity_type = EXCLUDED.parent_entity_type;"
        )
    return "\n".join(lines)


def generate() -> str:
    supertypes = _load_supertypes()
    leaves = _load_leaves()

    n_super = len(supertypes)
    n_leaves = sum(len(leaves.get(s, [])) for s in supertypes)
    header = (
        "-- ============================================================================\n"
        "-- kg event persistence — kgdb P2: seed the KG type catalog\n"
        "-- ============================================================================\n"
        "--\n"
        "-- Target: kgdb  (KGDB_URI)\n"
        "--\n"
        "-- GENERATED by kg/scripts/gen_kg_catalog_seed.py — do not edit by hand.\n"
        "-- Source: src/entities/extraction/schemas/*.json + catalogues/event_types.csv\n"
        "--\n"
        "-- Idempotent: ON CONFLICT (entity_type, entity_kind) DO UPDATE. Apply AFTER\n"
        "-- db/kg_db/inserts_catalog_tables.sql (needs the generic catalog + kinds).\n"
        f"-- Seeds {n_super} supertypes + {n_leaves} child types. Themes are skipped\n"
        "-- (entity_kinds_available has no 'theme' kind yet).\n"
        "-- ============================================================================\n"
    )

    blocks = [
        _supertype_block(supertype, supertypes[supertype], leaves.get(supertype, []))
        for supertype in sorted(supertypes)
    ]

    verification = (
        "\n-- ----------------------------------------------------------------------------\n"
        "-- Verification (read-only)\n"
        "-- ----------------------------------------------------------------------------\n"
        "--   SELECT entity_kind, count(*) FILTER (WHERE parent_entity_type IS NULL) AS supertypes,\n"
        "--          count(*) FILTER (WHERE parent_entity_type IS NOT NULL) AS children\n"
        "--   FROM public.entity_types_kinds_available\n"
        "--   WHERE metadata_template IS NOT NULL OR parent_entity_type IS NOT NULL\n"
        "--   GROUP BY entity_kind;\n"
        "--   SELECT entity_type FROM public.entity_types_kinds_available\n"
        "--   WHERE entity_type = 'paid_mass_event' AND metadata_template IS NOT NULL;\n"
    )

    return header + "\nBEGIN;\n\n" + "\n\n".join(blocks) + "\n\nCOMMIT;\n" + verification


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--stdout", action="store_true", help="print instead of writing the file")
    args = parser.parse_args()

    sql = generate()
    if args.stdout:
        print(sql)
        return
    _OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    _OUT_PATH.write_text(sql, encoding="utf-8")
    print(f"Wrote {_OUT_PATH}")


if __name__ == "__main__":
    main()
