"""Tests for KgdbWriter (src/entities/linking/persistence.py).

Uses a fake psycopg2 connection/cursor — no live DB. The fake cursor records
every execute(sql, params) call and returns canned fetchone/fetchall values
(as dicts, matching RealDictCursor)."""

from typing import Any, List, Optional, Tuple

from src.entities.linking.persistence import KgdbWriter


class FakeCursor:
    def __init__(self, fetch_plan=None):
        # fetch_plan: callable(sql, params) -> dict | None (for fetchone)
        self.calls: List[Tuple[str, Any]] = []
        self._fetch_plan = fetch_plan
        self._last_one: Optional[dict] = None
        self._last_all: List[dict] = []

    def execute(self, sql, params=None):
        self.calls.append((sql, params))
        self._last_one = None
        self._last_all = []
        if self._fetch_plan is not None:
            res = self._fetch_plan(sql, params)
            if isinstance(res, list):
                self._last_all = res
            else:
                self._last_one = res

    def fetchone(self):
        return self._last_one

    def fetchall(self):
        return self._last_all

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class FakeConn:
    def __init__(self, fetch_plan=None):
        self.cursor_obj = FakeCursor(fetch_plan)
        self.committed = 0
        self.rolled_back = 0

    def cursor(self, cursor_factory=None):
        return self.cursor_obj

    def commit(self):
        self.committed += 1

    def rollback(self):
        self.rolled_back += 1

    def close(self):
        pass


def _sql_texts(conn) -> List[str]:
    return [c[0] for c in conn.cursor_obj.calls]


# --- Fix B: existence check scope --------------------------------------------


def test_streaming_find_existing_omits_link_run():
    # Streaming upsert: existence check must be by _link_id ALONE.
    conn = FakeConn(fetch_plan=lambda sql, params: None)
    w = KgdbWriter("run-x", conn=conn)
    w._find_existing(conn.cursor_obj, "lid", run_scoped=False)
    sel = conn.cursor_obj.calls[0][0]
    assert "_link_id" in sel
    assert "_link_run" not in sel


def test_batch_find_existing_includes_link_run():
    conn = FakeConn(fetch_plan=lambda sql, params: None)
    w = KgdbWriter("run-x", conn=conn)
    w._find_existing(conn.cursor_obj, "lid", run_scoped=True)
    sel = conn.cursor_obj.calls[0][0]
    assert "_link_id" in sel
    assert "_link_run" in sel


def test_persist_streaming_path_check_is_not_run_scoped():
    # Drive upsert_linked; the FIRST execute is the existence SELECT.
    conn = FakeConn(fetch_plan=lambda sql, params: None)
    w = KgdbWriter("run-x", conn=conn)
    # record has no _supertype -> permanent drop after the existence check,
    # so only the existence SELECT runs.
    w.upsert_linked({"id": "L1"})
    sel = conn.cursor_obj.calls[0][0]
    assert "_link_id" in sel
    assert "_link_run" not in sel


def test_persist_batch_path_check_is_run_scoped():
    conn = FakeConn(fetch_plan=lambda sql, params: None)
    w = KgdbWriter("run-x", conn=conn)
    w.write_linked({"id": "L1"})
    sel = conn.cursor_obj.calls[0][0]
    assert "_link_id" in sel
    assert "_link_run" in sel


# --- Fix C: per-source doc_date_created + news_type --------------------------


def test_write_documents_uses_per_source_date_and_news_type():
    cur = FakeCursor()
    record = {
        "publication_date": "2026-01-01T00:00:00",  # canonical/earliest — must NOT be used
        "news_type": None,
        "source_ids": ["http://a/1", "http://b/2"],
        "_sources": [
            {
                "source_id": "http://a/1",
                "publication_date": "2026-01-05T00:00:00",
                "news_type": "ElUniversal",
            },
            {
                "source_id": "http://b/2",
                "publication_date": "2026-03-09T00:00:00",
                "news_type": "Milenio",
            },
        ],
    }
    KgdbWriter._write_documents(cur, 42, record)

    inserts = [c for c in cur.calls if "INSERT INTO entities_documents" in c[0]]
    assert len(inserts) == 2
    # params order: (entity_id, source_id, "news", date_created, host, news_type)
    by_doc = {c[1][1]: c[1] for c in inserts}
    assert by_doc["http://a/1"][3] == "2026-01-05T00:00:00"
    assert by_doc["http://a/1"][5] == "ElUniversal"
    assert by_doc["http://b/2"][3] == "2026-03-09T00:00:00"
    assert by_doc["http://b/2"][5] == "Milenio"


def test_write_documents_falls_back_to_canonical_without_sources():
    cur = FakeCursor()
    record = {
        "publication_date": "2026-01-01T00:00:00",
        "news_type": "ElUniversal",
        "source_ids": ["http://a/1"],
    }
    KgdbWriter._write_documents(cur, 7, record)
    inserts = [c for c in cur.calls if "INSERT INTO entities_documents" in c[0]]
    assert len(inserts) == 1
    assert inserts[0][1][3] == "2026-01-01T00:00:00"
    assert inserts[0][1][5] == "ElUniversal"
