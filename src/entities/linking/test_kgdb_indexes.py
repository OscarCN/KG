"""Static assertion that kgdb has indexes covering the candidate-retrieval predicates.

We cannot rely on a live DB in CI, so this parses the backend's `schema.sql`
(the source of truth for kgdb DDL) and asserts that the `CREATE INDEX`
statements covering the predicates used by `kgdb_retrieval.py` are present.

Predicates (see `KgdbCandidateIndex.lookup_candidates` / `KgdbRecordStore`):
- `entities`: `metadata->>'_link_id'`, `metadata->>'_supertype'` (partition).
- `event_properties`: `tstzrange(date_start, date_end) && ...` overlap.
- `entity_locations`: equality on `level_2_id..level_7_id`.
"""

from __future__ import annotations

import os
import re

import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))


def _find_schema() -> str | None:
    """Resolve the backend kgdb schema.sql robustly."""
    candidates = [
        # test is at kg/kg/src/entities/linking/ -> up 4 to media/, then into backend
        os.path.normpath(
            os.path.join(_HERE, "..", "..", "..", "..", "media-backend-paid",
                         "db", "kg_db", "schema.sql")
        ),
        # up 5, in case the workspace nests differently
        os.path.normpath(
            os.path.join(_HERE, "..", "..", "..", "..", "..", "media-backend-paid",
                         "db", "kg_db", "schema.sql")
        ),
    ]
    for path in candidates:
        if os.path.isfile(path):
            return path
    return None


@pytest.fixture(scope="module")
def schema_sql() -> str:
    path = _find_schema()
    if path is None:
        pytest.skip(
            "backend kgdb schema.sql not found relative to test "
            "(expected ../../../../media-backend-paid/db/kg_db/schema.sql)"
        )
    with open(path, "r", encoding="utf-8") as fh:
        return fh.read().lower()


def _create_index_statements(sql: str) -> list[str]:
    """All `CREATE INDEX ...;` statements (lowercased), whitespace-normalized."""
    stmts = re.findall(r"create\s+index[^;]*;", sql, flags=re.IGNORECASE | re.DOTALL)
    return [re.sub(r"\s+", " ", s) for s in stmts]


def test_link_id_index(schema_sql: str) -> None:
    stmts = _create_index_statements(schema_sql)
    assert any(
        "entities" in s and "metadata" in s and "_link_id" in s for s in stmts
    ), "missing CREATE INDEX on entities (metadata->>'_link_id')"


def test_supertype_partition_index(schema_sql: str) -> None:
    stmts = _create_index_statements(schema_sql)
    assert any(
        "entities" in s and "metadata" in s and "_supertype" in s for s in stmts
    ), "missing CREATE INDEX on entities (metadata->>'_supertype')"


def test_event_properties_date_range_index(schema_sql: str) -> None:
    stmts = _create_index_statements(schema_sql)
    assert any(
        "event_properties" in s and ("tstzrange" in s or "date_start" in s)
        for s in stmts
    ), "missing CREATE INDEX on event_properties date range (tstzrange/date_start)"


@pytest.mark.parametrize("level", ["level_2_id", "level_3_id", "level_5_id",
                                   "level_6_id", "level_7_id"])
def test_entity_locations_level_index(schema_sql: str, level: str) -> None:
    stmts = _create_index_statements(schema_sql)
    assert any(
        "entity_locations" in s and level in s for s in stmts
    ), f"missing CREATE INDEX on entity_locations ({level})"
