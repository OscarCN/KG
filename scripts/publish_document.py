"""Publish a JSON document to a RabbitMQ queue — dev/testing helper for the listener.

Reads a document file (a single object, or a list — first ``--count`` used), injects
a top-level ``trace_id``, and publishes each to ``RABBIT_QUEUE`` on the configured
broker/vhost. Pairs with ``src/listener.py`` for the dev-vhost loop (see
``dev/docs/db/runbook.md``).

Env (shell or .env.local): RABBIT_HOST/PORT/USER/PASSWORD/VIRTUALHOST/QUEUE.

Usage:
    RABBIT_QUEUE=dev_kg_documents python scripts/publish_document.py data/<dir>/<doc>.json
    python scripts/publish_document.py data/<dir>/<file>.json --count 3 --trace my-trace
"""

from __future__ import annotations

import argparse
import json
import os
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv

_PROJECT_ROOT = Path(__file__).resolve().parents[1]
load_dotenv(_PROJECT_ROOT / ".env.local")

import pika  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("doc", type=Path, help="JSON file: a document object or a list")
    parser.add_argument("--count", type=int, default=1, help="how many docs to publish from a list")
    parser.add_argument("--trace", default=None, help="trace_id (default: generated per doc)")
    args = parser.parse_args()

    payload = json.load(open(args.doc, encoding="utf-8"))
    docs = payload if isinstance(payload, list) else [payload]
    docs = docs[: args.count]

    queue = os.environ["RABBIT_QUEUE"]
    credentials = pika.PlainCredentials(os.environ["RABBIT_USER"], os.environ["RABBIT_PASSWORD"])
    connection = pika.BlockingConnection(
        pika.ConnectionParameters(
            host=os.environ["RABBIT_HOST"],
            port=int(os.environ.get("RABBIT_PORT", 5672)),
            virtual_host=os.environ.get("RABBIT_VIRTUALHOST", "/"),
            credentials=credentials,
        )
    )
    channel = connection.channel()
    channel.queue_declare(queue=queue, durable=True)
    for i, doc in enumerate(docs):
        trace = args.trace or f"dev-{datetime.now(timezone.utc):%Y%m%d-%H%M%S}-{i}"
        doc["trace_id"] = trace  # top level — read by the listener, ignored by record_to_article
        channel.basic_publish(
            exchange="",
            routing_key=queue,
            body=json.dumps(doc).encode("utf-8"),
            properties=pika.BasicProperties(delivery_mode=2),
        )
        print(f"published trace={trace} -> {queue} (doc {i + 1}/{len(docs)})")
    connection.close()


if __name__ == "__main__":
    main()
