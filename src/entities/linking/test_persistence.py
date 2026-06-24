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
