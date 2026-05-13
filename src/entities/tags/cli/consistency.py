"""CLI: run a consistency pass over an existing run snapshot.

Usage:
    python -m src.entities.tags.cli.consistency \\
        --customer data/tags/customer_75.json \\
        --catalog  data/tags/customer_75/run_<ts>.json \\
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

from src.entities.tags.runner import LocalRunConfig, run_local_consistency


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--customer", required=True, type=Path)
    parser.add_argument("--catalog", required=True, type=Path,
                        help="streaming snapshot (data/tags/<slug>/run_<ts>.json)")
    parser.add_argument("--out-dir", required=True, type=Path)
    args = parser.parse_args()

    config = LocalRunConfig(
        customer_path=args.customer,
        # linked/events not used by consistency; pass placeholders.
        linked_path=Path("/dev/null"),
        events_path=Path("/dev/null"),
        output_dir=args.out_dir,
        catalog_path=args.catalog,
    )
    run_local_consistency(config)
    return 0


if __name__ == "__main__":
    sys.exit(main())
