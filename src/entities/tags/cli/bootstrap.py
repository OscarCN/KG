"""CLI: build the per-customer typed stance catalog from a pre-linked corpus.

Usage:
    python -m src.entities.tags.cli.bootstrap \\
        --customer data/tags/customer_75.json \\
        --corpus   data/linked/<file>.json \\
        --events   data/linked/<file>__events.json \\
        --out-dir  data/tags/
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

from dotenv import load_dotenv

_PROJECT_ROOT = Path(__file__).resolve().parents[4]
load_dotenv(_PROJECT_ROOT / ".env.local")

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
logging.getLogger("src.entities.tags").setLevel(logging.INFO)

from src.entities.tags.runner import LocalRunConfig, run_local_bootstrap


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--customer", required=True, type=Path)
    parser.add_argument("--corpus", required=True, type=Path,
                        help="pre-linked fixture (data/linked/<stem>.json)")
    parser.add_argument("--events", default=None, type=Path,
                        help="event store (defaults to <corpus stem>__events.json)")
    parser.add_argument("--out-dir", required=True, type=Path)
    parser.add_argument("--catalog-out", default=None, type=Path,
                        help="explicit output path; defaults to <out-dir>/<slug>/bootstrap.json")
    parser.add_argument("--limit", default=None, type=int,
                        help="cap the corpus to first N bundles (debug)")
    args = parser.parse_args()

    events_path = args.events or args.corpus.with_name(f"{args.corpus.stem}__events.json")

    config = LocalRunConfig(
        customer_path=args.customer,
        linked_path=args.corpus,
        events_path=events_path,
        output_dir=args.out_dir,
        catalog_path=args.catalog_out,
        bootstrap_corpus_limit=args.limit,
    )
    run_local_bootstrap(config)
    return 0


if __name__ == "__main__":
    sys.exit(main())
