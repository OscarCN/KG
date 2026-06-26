"""Ontology rule-loading from kgdb (source="db").

Uses a fake psycopg2 connection/cursor — no live DB. The DB stores RAW
(human-editable) rule values; the loader must normalize them IDENTICALLY to the
xlsx path (lowercase + strip accents for kw/phrase/not, strip for categories,
lowercase for document_type) so matching behaves the same.
"""

from typing import Any, List, Tuple

from src.entities.extraction.extract import Ontology

# columns the loader selects, in order:
# ontology_class, kw, phrase, not_kw, categories, dismiss_categories, document_type
_ROWS = [
    ("robbery", ["Robo", "Asaltó"], [], ["broma"], ["Seguridad"], [], ["News"]),
]


class _Cur:
    def __init__(self, rows):
        self._rows = rows
        self.last_sql = None

    def execute(self, sql, params=None):
        self.last_sql = sql

    def fetchall(self):
        return self._rows

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class _Conn:
    def __init__(self, rows):
        self.cur = _Cur(rows)

    def cursor(self, *a, **k):
        return self.cur

    def close(self):
        pass


def test_load_rules_from_db_normalizes_like_xlsx():
    conn = _Conn(_ROWS)
    onto = Ontology(source="db", conn=conn)
    assert len(onto.rules) == 1
    r = onto.rules[0]
    assert r["ontology_class"] == "robbery"
    # kw/not normalized: lowercased + accents stripped
    assert r["kw"] == ["robo", "asalto"]
    assert r["not"] == ["broma"]
    # categories kept as-is (stripped, case preserved)
    assert r["categories"] == ["Seguridad"]
    # document_type lowercased
    assert r["document_type"] == ["news"]
    # stemming precomputed
    assert "kw_stemmed" in r and len(r["kw_stemmed"]) == 2
    # query filters enabled rules
    assert "enabled" in (conn.cur.last_sql or "").lower()


def test_db_sourced_ontology_matches():
    onto = Ontology(source="db", conn=_Conn(_ROWS))
    hits = onto.match(text="Anoche hubo un ROBO en la tienda",
                      categories=["Seguridad"], document_type="news")
    assert "robbery" in hits
    # category gate excludes when missing
    assert onto.match(text="hubo un robo", categories=["Deportes"],
                      document_type="news") == set()
