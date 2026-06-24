"""Streaming RabbitMQ consumer for the KG pipeline.

Consumes raw documents from a queue and runs the full pipeline inline per message
— classify -> extract -> link -> persist — i.e. the ``run_entities.py`` loop
wrapped in a pika ``BlockingConnection`` consumer (modeled on
``social_tags/src/stream.py`` and ``ai_assist/src/stream.py``). Created/merged
linked records are written to kgdb via ``KgdbWriter.upsert_linked``; records the
linker ``skipped`` (themes / entities not linked yet) and drops are counted, not
persisted.

Failure handling: a malformed body dead-letters immediately
(``nack(requeue=False)``); other (retryable) failures — e.g. kgdb / geocoder /
OpenRouter unreachable — sleep ``retry_delay_seconds`` and requeue, up to
``max_retries`` per message, then dead-letter. ``upsert_linked`` re-raises DB
errors precisely so they requeue rather than silently drop.

State caveat: the linker's ``CandidateIndex`` is in-memory, so a single
long-lived worker holds dedup state only for its lifetime. Cross-restart /
multi-worker correctness needs the kgdb-backed ``CandidateIndex`` — see
``docs/todos/kgdb_event_persistence.md`` ("Streaming consumer").

Env:
  RabbitMQ  RABBIT_HOST/PORT/USER/PASSWORD/VIRTUALHOST/EXCHANGE/QUEUE/ROUTING_KEY,
            RABBIT_PREFETCH_COUNT, RABBIT_RETRY_DELAY_SECONDS, RABBIT_MAX_RETRIES, RABBIT_DLX
  kgdb      KGDB_HOST/PORT/USER/PASSWORD/NAME
  pipeline  OPENROUTER_API_KEY, NLP_URL, GEOCODING_URL
  KG_RUN_TAG  optional provenance tag stored on metadata._link_run (default "stream")

Run:
  python src/listener.py                       # consume the configured queue
  python src/listener.py --once data/<dir>/<doc>.json   # offline smoke test (no RabbitMQ)
"""

from __future__ import annotations

import argparse
import functools
import json
import logging
import os
import signal
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

from dotenv import load_dotenv

_PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))
load_dotenv(_PROJECT_ROOT / ".env.local")

import pika  # noqa: E402

from src.entities.document import record_to_article  # noqa: E402
from src.entities.extraction.extract import EntityExtractor, Ontology  # noqa: E402
from src.entities.linking.kgdb_retrieval import (  # noqa: E402
    KgdbCandidateIndex,
    KgdbRecordStore,
)
from src.entities.linking.link import EntityLinker  # noqa: E402
from src.entities.linking.persistence import KgdbWriter  # noqa: E402
from src.processed_store import ProcessedStore  # noqa: E402

logger = logging.getLogger(__name__)


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    return int(raw) if raw not in (None, "") else default


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name)
    return float(raw) if raw not in (None, "") else default


def configure_logging() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    for noisy in ("pika", "urllib3"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


@dataclass(frozen=True)
class RabbitConfig:
    host: str
    port: int
    user: str
    password: str
    virtual_host: str
    exchange: str
    queue: str
    routing_key: str
    prefetch_count: int = 1
    retry_delay_seconds: float = 3.0
    max_retries: int = 5
    dead_letter_exchange: Optional[str] = None

    @classmethod
    def from_env(cls) -> "RabbitConfig":
        queue = os.environ["RABBIT_QUEUE"]
        return cls(
            host=os.environ["RABBIT_HOST"],
            port=_env_int("RABBIT_PORT", 5672),
            user=os.environ["RABBIT_USER"],
            password=os.environ["RABBIT_PASSWORD"],
            virtual_host=os.environ.get("RABBIT_VIRTUALHOST") or "/",
            exchange=os.environ.get("RABBIT_EXCHANGE") or "",
            queue=queue,
            routing_key=os.environ.get("RABBIT_ROUTING_KEY") or queue,
            prefetch_count=_env_int("RABBIT_PREFETCH_COUNT", 1),
            retry_delay_seconds=_env_float("RABBIT_RETRY_DELAY_SECONDS", 3.0),
            max_retries=_env_int("RABBIT_MAX_RETRIES", 5),
            dead_letter_exchange=os.environ.get("RABBIT_DLX") or None,
        )


class KgPipeline:
    """Stateful per-worker pipeline: extract -> link -> persist one document.

    The extractor, linker (with its in-memory CandidateIndex), and KgdbWriter
    live for the worker's lifetime — that's what keeps dedup state across
    messages. ``process`` raises on transient persistence failures so the caller
    can requeue.
    """

    def __init__(self, *, geocode: bool = True, run_tag: str = "stream",
                 writer: Optional[KgdbWriter] = None):
        self.extractor = EntityExtractor(ontology=Ontology())
        self.writer = writer or KgdbWriter(run_tag=run_tag)
        # Candidate lookup + record resolution read from kgdb (durable dedup
        # across restarts / workers), on a SEPARATE autocommit connection so
        # reads see every committed upsert and never sit in the writer's tx.
        self._read_conn = KgdbWriter._connect()
        self._read_conn.autocommit = True
        self.linker = EntityLinker(
            geocode=geocode,
            index=KgdbCandidateIndex(self._read_conn),
            record_store=KgdbRecordStore(self._read_conn),
        )
        self.documents = 0

    def process(self, message: dict, trace_id: Optional[str] = None) -> dict:
        article = record_to_article(message)
        source_id = article.get("id")
        self.documents += 1

        matched = self.extractor.match(article)
        if not matched:
            logger.info("trace=%s doc=%s no_match", trace_id, source_id)
            return {"source_id": source_id, "extracted": 0, "persisted": 0, "statuses": {}}

        extracted = self.extractor.extract(
            article, validate=True, raise_validation_error=False
        )
        statuses: dict[str, int] = {}
        persisted = 0
        for raw in extracted:
            result = self.linker.link_one(raw)
            statuses[result.status] = statuses.get(result.status, 0) + 1
            if result.status in ("created", "merged") and result.record is not None:
                # Raises on DB error -> message requeues; None only for poison drops.
                if self.writer.upsert_linked(result.record) is not None:
                    persisted += 1

        logger.info(
            "trace=%s doc=%s extracted=%d persisted=%d statuses=%s",
            trace_id, source_id, len(extracted), persisted, statuses,
        )
        return {
            "source_id": source_id,
            "extracted": len(extracted),
            "persisted": persisted,
            "statuses": statuses,
        }

    def close(self) -> None:
        self.writer.close()
        if self._read_conn is not None:
            self._read_conn.close()
            self._read_conn = None


class DocumentListener:
    """pika BlockingConnection consumer driving ``KgPipeline`` per message."""

    def __init__(self, *, rabbit_config: RabbitConfig, pipeline: KgPipeline,
                 processed: Optional[ProcessedStore] = None):
        self.cfg = rabbit_config
        self.pipeline = pipeline
        self.processed = processed  # document-level dedup guard (None = disabled)
        self.channel = None
        self._retry_counts: dict[str, int] = {}

    def _connect(self):
        credentials = pika.PlainCredentials(self.cfg.user, self.cfg.password)
        params = pika.ConnectionParameters(
            host=self.cfg.host,
            port=self.cfg.port,
            credentials=credentials,
            virtual_host=self.cfg.virtual_host,
            heartbeat=600,
            blocked_connection_timeout=300,
        )
        return pika.BlockingConnection(params)

    @staticmethod
    def _queue_arguments(dead_letter_exchange: Optional[str]) -> Optional[dict[str, Any]]:
        return {"x-dead-letter-exchange": dead_letter_exchange} if dead_letter_exchange else None

    def build_listener(self):
        rabbit = self._connect()
        self.channel = rabbit.channel()
        if self.cfg.exchange:
            self.channel.exchange_declare(exchange=self.cfg.exchange, durable=True)
        self.channel.queue_declare(
            queue=self.cfg.queue,
            durable=True,
            arguments=self._queue_arguments(self.cfg.dead_letter_exchange),
        )
        if self.cfg.exchange:
            self.channel.queue_bind(
                exchange=self.cfg.exchange,
                queue=self.cfg.queue,
                routing_key=self.cfg.routing_key,
            )
        self.channel.basic_qos(prefetch_count=self.cfg.prefetch_count)
        callback = functools.partial(self.process_document, args=(rabbit,))
        self.channel.basic_consume(queue=self.cfg.queue, on_message_callback=callback)
        finish = functools.partial(self.finish_process, args=(rabbit, self.channel))
        signal.signal(signal.SIGTERM, finish)
        return finish

    def process_document(self, channel, method, properties, body, args) -> None:
        del properties, args
        identity = str(method.delivery_tag)
        doc_id = ""
        try:
            message = json.loads(body)
            trace_id = message.get("trace_id") if isinstance(message, dict) else None
            if isinstance(message, dict):
                doc_id = str(message.get("_id") or message.get("url") or "")
                identity = doc_id or str(method.delivery_tag)
            # Document-level idempotency: skip docs already fully processed.
            if doc_id and self.processed is not None and self.processed.seen(doc_id):
                logger.info("skip already-processed doc=%s", doc_id)
                self._ack_or_nack(channel, method.delivery_tag, ack=True, requeue=False)
                return
            self.pipeline.process(message, trace_id=trace_id)
            # Mark processed only after success (failures requeue and retry).
            if doc_id and self.processed is not None:
                self.processed.mark(doc_id)
        except (json.JSONDecodeError, ValueError):
            logger.exception("malformed message; dead-lettering tag=%s", method.delivery_tag)
            self._ack_or_nack(channel, method.delivery_tag, ack=False, requeue=False)
            return
        except Exception:
            attempts = self._retry_counts.get(identity, 0) + 1
            self._retry_counts[identity] = attempts
            if attempts >= self.cfg.max_retries:
                logger.error(
                    "message failed %d times; dead-lettering tag=%s body=%r",
                    attempts, method.delivery_tag, body,
                )
                self._retry_counts.pop(identity, None)
                self._ack_or_nack(channel, method.delivery_tag, ack=False, requeue=False)
                return
            logger.exception("retryable failure (attempt %d); requeueing after delay", attempts)
            time.sleep(self.cfg.retry_delay_seconds)
            self._ack_or_nack(channel, method.delivery_tag, ack=False, requeue=True)
            return
        self._retry_counts.pop(identity, None)
        self._ack_or_nack(channel, method.delivery_tag, ack=True, requeue=False)

    @staticmethod
    def _ack_or_nack(channel, delivery_tag, *, ack: bool, requeue: bool) -> None:
        if not channel.is_open:
            logger.warning("channel closed before ack/nack tag=%s", delivery_tag)
            return
        if ack:
            channel.basic_ack(delivery_tag)
        else:
            channel.basic_nack(delivery_tag, requeue=requeue)

    def finish_process(self, signal_number, frame, args) -> None:
        del signal_number, frame
        connection, channel = args
        if channel.is_open:
            channel.stop_consuming()
        connection.close()
        self.pipeline.close()
        sys.exit()

    def start_listening(self) -> None:
        finish = self.build_listener()
        logger.info("listening on vhost=%s queue=%s", self.cfg.virtual_host, self.cfg.queue)
        try:
            self.channel.start_consuming()
        except KeyboardInterrupt:
            finish(0, None)


def _run_once(path: Path, *, geocode: bool, limit: Optional[int]) -> dict:
    """Offline smoke test: run the pipeline over a document fixture, no RabbitMQ."""
    payload = json.load(open(path, encoding="utf-8"))
    documents = payload if isinstance(payload, list) else [payload]
    if limit:
        documents = documents[:limit]
    pipeline = KgPipeline(geocode=geocode, run_tag=os.environ.get("KG_RUN_TAG", "stream"))
    totals = {"documents": 0, "extracted": 0, "persisted": 0}
    try:
        for message in documents:
            summary = pipeline.process(message, trace_id=message.get("trace_id"))
            totals["documents"] += 1
            totals["extracted"] += summary["extracted"]
            totals["persisted"] += summary["persisted"]
    finally:
        pipeline.close()
    print(f"--once {path.name}: {totals}")
    return totals


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--once", type=Path, help="run the pipeline over a fixture file (no RabbitMQ)")
    parser.add_argument("--limit", type=int, default=None, help="max docs for --once")
    parser.add_argument("--no-geocode", action="store_true", help="disable geocoding")
    args = parser.parse_args()

    configure_logging()
    if args.once:
        _run_once(args.once, geocode=not args.no_geocode, limit=args.limit)
        return 0

    pipeline = KgPipeline(
        geocode=not args.no_geocode,
        run_tag=os.environ.get("KG_RUN_TAG", "stream"),
    )
    listener = DocumentListener(
        rabbit_config=RabbitConfig.from_env(),
        pipeline=pipeline,
        processed=ProcessedStore.from_env(),
    )
    listener.start_listening()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
