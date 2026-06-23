"""Persist a linked-events fixture into the kgdb Postgres DB (Step Zero).

Loads ``data/linked/<stem>.json`` (the linker's ``{"events": [...]}`` output) and
writes each event into kgdb via ``KgdbWriter.write_linked`` — one transaction per
record. Idempotent: re-running the same file is a no-op (records are skipped by
their ``_link_id``); pass ``--reset`` to delete the prior run first (needed after
re-running the linker, which mints new ids).

Usage:
    LINK_STEM=geo_qro_paid_mass_event python scripts/persist_linked.py --reset
    python scripts/persist_linked.py geo_qro_paid_mass_event

Required env (via .env.local or shell):
    KGDB_HOST, KGDB_PORT, KGDB_USER, KGDB_PASSWORD, KGDB_NAME
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from pathlib import Path

from dotenv import load_dotenv

_PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))
load_dotenv(_PROJECT_ROOT / ".env.local")

from src.entities.linking.persistence import KgdbWriter  # noqa: E402

logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")


def _load_events(stem: str) -> list[dict]:
    path = _PROJECT_ROOT / "data" / "linked" / f"{stem}.json"
    if not path.exists():
        raise SystemExit(f"fixture not found: {path}")
    payload = json.load(open(path, encoding="utf-8"))
    events = payload.get("events") if isinstance(payload, dict) else payload
    if not isinstance(events, list):
        raise SystemExit(f"expected an events list in {path}")
    return events


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "stem",
        nargs="?",
        default=os.environ.get("LINK_STEM", "geo_qro_paid_mass_event"),
        help="fixture stem under data/linked/ (or LINK_STEM env)",
    )
    parser.add_argument("--reset", action="store_true", help="delete the prior run first")
    args = parser.parse_args()

    events = _load_events(args.stem)
    print(f"Loaded {len(events)} linked events from {args.stem}.json")

    writer = KgdbWriter(run_tag=args.stem)
    try:
        if args.reset:
            removed = writer.reset_run()
            print(f"--reset: removed {removed} entities from a prior run")

        id_map: dict[str, int] = {}
        for event in events:
            entity_id = writer.write_linked(event)
            if entity_id is not None:
                id_map[str(event.get("id"))] = entity_id

        print(
            f"written={writer.written} skipped={writer.skipped} "
            f"dropped={dict(writer.dropped)}"
        )
        sample = list(id_map.items())[:5]
        print(f"_link_id -> entity_id (first {len(sample)}): {sample}")
    finally:
        writer.close()


if __name__ == "__main__":
    main()
