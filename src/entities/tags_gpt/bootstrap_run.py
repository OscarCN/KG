"""CLI bootstrap entry point for tags_gpt."""

from __future__ import annotations

import argparse
from pathlib import Path

from src.entities.tags_gpt.bootstrap import StanceBootstrapStep
from src.entities.tags_gpt.llm import default_cached_llm
from src.entities.tags_gpt.persistence import load_customer, save_bootstrap
from src.entities.tags_gpt.retrieval import LinkedJsonRetriever


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--customer", required=True, type=Path, help="Customer fixture JSON path")
    parser.add_argument("--corpus", required=True, type=Path, help="Pre-linked corpus JSON path")
    parser.add_argument("--events", type=Path, default=None, help="Optional sibling event context JSON path")
    parser.add_argument("--out", type=Path, default=None, help="Bootstrap output path")
    args = parser.parse_args()

    customer = load_customer(args.customer)
    retriever = LinkedJsonRetriever(args.corpus, args.events)
    catalog = StanceBootstrapStep(default_cached_llm()).bootstrap(
        customer,
        retriever.get_customer_corpus(customer),
    )
    out = args.out or Path("data") / "tags" / customer.slug / "bootstrap.json"
    save_bootstrap(out, catalog)
    print(f"wrote {out}")


if __name__ == "__main__":
    main()

