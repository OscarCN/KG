"""CLI consistency-pass entry point for tags_gpt."""

from __future__ import annotations

import argparse
import json
from dataclasses import asdict
from pathlib import Path

from src.entities.tags_gpt.consistency import ConsistencyPassStep
from src.entities.tags_gpt.llm import default_cached_llm
from src.entities.tags_gpt.models import json_default
from src.entities.tags_gpt.persistence import load_customer, load_snapshot, save_snapshot


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--customer", required=True, type=Path, help="Customer fixture JSON path")
    parser.add_argument("--catalog", required=True, type=Path, help="Run snapshot path")
    parser.add_argument("--out", type=Path, default=None, help="Consistency result output path")
    args = parser.parse_args()

    customer = load_customer(args.customer)
    stance_catalog, claim_catalogs = load_snapshot(args.catalog)
    result = ConsistencyPassStep(customer, default_cached_llm()).run(
        stance_catalog,
        claim_catalogs=claim_catalogs,
    )
    save_snapshot(args.catalog, stance_catalog=stance_catalog, claim_catalogs=claim_catalogs)
    out = args.out or args.catalog.with_name(f"{args.catalog.stem}__consistency.json")
    with open(out, "w", encoding="utf-8") as handle:
        json.dump(asdict(result), handle, ensure_ascii=False, indent=2, default=json_default)
    print(f"wrote {out}")


if __name__ == "__main__":
    main()
