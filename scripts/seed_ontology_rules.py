"""Seed kgdb `ontology_matching_rules` from the keywords.xlsx catalogue.

Full refresh: TRUNCATE then insert every row of the Excel (including disabled
ones, with their `enabled` flag and label columns), storing RAW human-editable
values — the Ontology DB loader normalizes them at read time, identically to the
xlsx path. Run once to migrate, and again whenever the Excel is the source of a
change (until editing moves to the DB / an admin UI).

Connection via KGDB_HOST/PORT/USER/PASSWORD/NAME (loaded from kg/.env.local).

Usage:
    python scripts/seed_ontology_rules.py --dry-run     # counts only, no write
    python scripts/seed_ontology_rules.py               # TRUNCATE + insert
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

import pandas as pd
from dotenv import load_dotenv

_PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))
load_dotenv(_PROJECT_ROOT / ".env.local")

from src.entities.linking.persistence import KgdbWriter  # noqa: E402

_XLSX = _PROJECT_ROOT / "src/entities/extraction/catalogues/keywords.xlsx"


def _cell(row, col):
    v = row.get(col)
    return None if pd.isna(v) else str(v)


def _split_quoted(value):
    """Raw split of a '"a","b"' cell — no normalization (loader normalizes)."""
    if not value or not value.strip():
        return []
    items = re.findall(r'"([^"]+)"', value)
    if not items:
        items = [s.strip() for s in value.split(",")]
    return [s.strip() for s in items if s.strip()]


def _split(value, sep):
    if not value or not value.strip():
        return []
    return [s.strip() for s in value.split(sep) if s.strip()]


def _to_bool(value) -> bool:
    if pd.isna(value):
        return True  # missing → enabled (xlsx backward-compat)
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in ("true", "1", "yes", "y", "si", "sí")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--dry-run", action="store_true", help="counts only, no write")
    args = ap.parse_args()

    df = pd.read_excel(_XLSX)
    rows = []
    for _, r in df.iterrows():
        cls = _cell(r, "class")
        if not cls or not cls.strip():
            continue
        rows.append((
            cls.strip(),
            _to_bool(r.get("enabled")),
            _split_quoted(_cell(r, "kw")),
            _split_quoted(_cell(r, "phrase")),
            _split_quoted(_cell(r, "not")),
            _split(_cell(r, "categories"), "|"),
            _split(_cell(r, "dismiss_categories"), "|"),
            _split(_cell(r, "document_type"), ","),
            _cell(r, "section"), _cell(r, "subsection"), _cell(r, "tag"),
            _cell(r, "location"), _cell(r, "period"), _cell(r, "bbox"),
            _cell(r, "comments"),
        ))

    enabled = sum(1 for x in rows if x[1])
    print(f"parsed {len(rows)} rules from {_XLSX.name} "
          f"({enabled} enabled, {len(rows) - enabled} disabled)")
    if args.dry_run:
        print("dry-run: nothing written")
        return

    conn = KgdbWriter._connect()
    try:
        with conn.cursor() as cur:
            cur.execute("TRUNCATE TABLE ontology_matching_rules RESTART IDENTITY")
            cur.executemany(
                "INSERT INTO ontology_matching_rules "
                "(ontology_class, enabled, kw, phrase, not_kw, categories, "
                " dismiss_categories, document_type, section, subsection, tag, "
                " location, period, bbox, comments) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                rows,
            )
        conn.commit()
        with conn.cursor() as cur:
            cur.execute("SELECT count(*), count(*) FILTER (WHERE enabled) FROM ontology_matching_rules")
            total, en = cur.fetchone()
        print(f"seeded ontology_matching_rules: {total} rows ({en} enabled)")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
