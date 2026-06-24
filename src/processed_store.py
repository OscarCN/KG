"""Redis-backed set of processed document ids — a document-level idempotency guard.

The listener marks a document id as processed (with a TTL) once it has been fully
extracted/linked/persisted and acked, and skips any document already marked. This
avoids re-extracting a redelivered or re-enqueued document — cheaper than relying
on the linker's kgdb dedup, and it sidesteps the noloc re-link duplicate edge case.

Per-document keys with `EX` (not a single hash): each id gets its own TTL, which a
Redis hash can't do on 5.x (`HEXPIRE` is 7.4+).

Env: REDIS_HOST, REDIS_PORT (default 6379), REDIS_PASSWORD,
     KG_PROCESSED_TTL_SECONDS (default 2 weeks).
"""

from __future__ import annotations

import logging
import os
from typing import Optional

import redis

logger = logging.getLogger(__name__)

TWO_WEEKS_SECONDS = 14 * 24 * 3600


class ProcessedStore:
    """Redis set of processed doc ids, keyed `kg:processed:<doc_id>` with a TTL."""

    def __init__(self, *, ttl_seconds: int = TWO_WEEKS_SECONDS,
                 prefix: str = "kg:processed:", client=None):
        self._ttl = ttl_seconds
        self._prefix = prefix
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
        return cls(ttl_seconds=ttl)

    def _key(self, doc_id: str) -> str:
        return f"{self._prefix}{doc_id}"

    def seen(self, doc_id: str) -> bool:
        return bool(doc_id) and bool(self._redis.exists(self._key(doc_id)))

    def mark(self, doc_id: str) -> None:
        if doc_id:
            self._redis.set(self._key(doc_id), 1, ex=self._ttl)
