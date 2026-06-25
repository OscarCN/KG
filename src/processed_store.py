"""Redis-backed document-level idempotency guard with atomic in-flight claims.

Two key namespaces give a single document a small state machine so that two
duplicate deliveries (multi-worker) cannot both be processed:

- `kg:processing:<doc_id>` — short-TTL in-flight claim. `claim()` does
  `SET NX EX`, so exactly one worker wins; everyone else (and anyone seeing a
  doc already PROCESSED) is rejected. The TTL bounds how long a crashed worker
  holds the claim before another delivery can re-claim.
- `kg:processed:<doc_id>` — long-TTL terminal marker set by `mark()` once the
  document is fully extracted/linked/persisted and acked.

Flow in the listener: `claim()` → process → `mark()` (success, which also clears
the processing claim) or `release()` (retryable failure, so the requeued
delivery can re-claim). A doc that is already PROCESSED, or currently claimed as
PROCESSING by another worker, is not claimable.

Per-document keys with `EX` (not a single hash): each id gets its own TTL, which
a Redis hash can't do on 5.x (`HEXPIRE` is 7.4+).

Env: REDIS_HOST, REDIS_PORT (default 6379), REDIS_PASSWORD,
     KG_PROCESSED_TTL_SECONDS (processed marker, default 2 weeks),
     KG_PROCESSING_TTL_SECONDS (in-flight claim, default 600s).
"""

from __future__ import annotations

import logging
import os
from typing import Optional

import redis

logger = logging.getLogger(__name__)

TWO_WEEKS_SECONDS = 14 * 24 * 3600
PROCESSING_TTL_SECONDS = 600


class ProcessedStore:
    """Doc-id idempotency: `kg:processing:<id>` claim + `kg:processed:<id>` marker."""

    def __init__(self, *, ttl_seconds: int = TWO_WEEKS_SECONDS,
                 processing_ttl_seconds: int = PROCESSING_TTL_SECONDS,
                 prefix: str = "kg:processed:",
                 processing_prefix: str = "kg:processing:", client=None):
        self._ttl = ttl_seconds
        self._processing_ttl = processing_ttl_seconds
        self._prefix = prefix
        self._processing_prefix = processing_prefix
        self._redis = client if client is not None else self._connect()

    @staticmethod
    def _connect():
        host = os.environ["REDIS_HOST"]
        port = int(os.environ.get("REDIS_PORT", 6379))
        password = os.environ.get("REDIS_PASSWORD") or None

        def _client(pw):
            return redis.Redis(
                host=host, port=port, password=pw,
                decode_responses=True, socket_timeout=5,
            )

        client = _client(password)
        try:
            client.ping()  # fail fast if Redis is configured but unreachable
        except redis.AuthenticationError:
            # Server has no auth configured but a password was supplied — retry
            # without it (dev Redis has no auth; prod does). A wrong password on
            # an auth'd server still fails on the retry, as it should.
            client = _client(None)
            client.ping()
        return client

    @classmethod
    def from_env(cls) -> "Optional[ProcessedStore]":
        """Build from env, or None (dedup disabled) when REDIS_HOST is unset."""
        if not os.environ.get("REDIS_HOST"):
            logger.warning("REDIS_HOST not set — document-level dedup disabled")
            return None
        ttl = int(os.environ.get("KG_PROCESSED_TTL_SECONDS") or TWO_WEEKS_SECONDS)
        processing_ttl = int(
            os.environ.get("KG_PROCESSING_TTL_SECONDS") or PROCESSING_TTL_SECONDS
        )
        return cls(ttl_seconds=ttl, processing_ttl_seconds=processing_ttl)

    def _key(self, doc_id: str) -> str:
        return f"{self._prefix}{doc_id}"

    def _processing_key(self, doc_id: str) -> str:
        return f"{self._processing_prefix}{doc_id}"

    def seen(self, doc_id: str) -> bool:
        return bool(doc_id) and bool(self._redis.exists(self._key(doc_id)))

    def claim(self, doc_id: str) -> bool:
        """Atomically claim a doc for processing.

        Returns True only if the caller won the claim: the doc is not already
        PROCESSED and not already PROCESSING. Uses SET NX EX (short TTL) so the
        win is atomic across workers; a crashed holder's claim expires.
        """
        if not doc_id:
            return False
        if self.seen(doc_id):
            return False
        won = self._redis.set(
            self._processing_key(doc_id), 1, nx=True, ex=self._processing_ttl
        )
        return bool(won)

    def release(self, doc_id: str) -> None:
        """Drop the in-flight claim so a requeued delivery can re-claim."""
        if doc_id:
            self._redis.delete(self._processing_key(doc_id))

    def mark(self, doc_id: str) -> None:
        """Mark a doc PROCESSED (long TTL) and clear its in-flight claim."""
        if doc_id:
            self._redis.set(self._key(doc_id), 1, ex=self._ttl)
            self._redis.delete(self._processing_key(doc_id))
