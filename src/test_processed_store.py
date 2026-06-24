"""Tests for ProcessedStore atomic in-flight claim semantics (no real Redis)."""

from src.processed_store import ProcessedStore


class FakeRedis:
    """Minimal dict-backed Redis fake implementing the methods ProcessedStore uses."""

    def __init__(self):
        self.store = {}

    def set(self, key, value, nx=False, ex=None):
        if nx and key in self.store:
            return None
        self.store[key] = value
        return True

    def exists(self, key):
        return 1 if key in self.store else 0

    def delete(self, key):
        return 1 if self.store.pop(key, None) is not None else 0

    def get(self, key):
        return self.store.get(key)


def _store():
    return ProcessedStore(client=FakeRedis())


def test_claim_atomic_nx():
    s = _store()
    assert s.claim("doc1") is True
    assert s.claim("doc1") is False


def test_claim_false_after_mark():
    s = _store()
    assert s.claim("doc2") is True
    s.mark("doc2")
    # A doc already marked PROCESSED must not be claimable again.
    assert s.claim("doc2") is False


def test_release_allows_reclaim():
    s = _store()
    assert s.claim("doc3") is True
    s.release("doc3")
    assert s.claim("doc3") is True
