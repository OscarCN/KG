"""CLI: stream the pre-linked corpus through the tags pipeline.

Usage:
    python -m src.entities.tags.cli.run \\
        --customer data/tags/customer_75.json \\
        --corpus   data/linked/<file>.json \\
        --events   data/linked/<file>__events.json \\
        --catalog  data/tags/customer_75/bootstrap.json \\
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

from src.entities.tags.runner import LocalRunConfig, run_local_stream


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--customer", required=True, type=Path)
    parser.add_argument("--corpus", required=True, type=Path)
    parser.add_argument("--events", default=None, type=Path)
    parser.add_argument("--catalog", default=None, type=Path,
                        help="bootstrap output to load; if absent starts from empty catalog")
    parser.add_argument("--out-dir", required=True, type=Path)
    parser.add_argument("--include-comments", action="store_true",
                        help="extract claims from comments too (default: roots only)")
    parser.add_argument("--top-n", type=int, default=10)
    args = parser.parse_args()

    events_path = args.events or args.corpus.with_name(f"{args.corpus.stem}__events.json")

    config = LocalRunConfig(
        customer_path=args.customer,
        linked_path=args.corpus,
        events_path=events_path,
        output_dir=args.out_dir,
        catalog_path=args.catalog,
        include_comments=args.include_comments,
        snapshot_top_n=args.top_n,
    )
    run_local_stream(config)
    return 0


if __name__ == "__main__":
    sys.exit(main())
