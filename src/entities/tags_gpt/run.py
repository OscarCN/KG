"""CLI streaming entry point for tags_gpt."""

from __future__ import annotations

import argparse
from datetime import datetime
from pathlib import Path

from src.entities.tags_gpt.catalogs import ClaimCatalogStore
from src.entities.tags_gpt.llm import default_cached_llm
from src.entities.tags_gpt.persistence import load_bootstrap, load_customer, save_snapshot
from src.entities.tags_gpt.retrieval import LinkedJsonRetriever
from src.entities.tags_gpt.streaming import StreamingState, StreamingTagsPipeline
from src.entities.tags_gpt.tagging import ClaimTagger, ClaimUpdater, StanceTagger, StanceUpdater, TypeTriageStep


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--customer", required=True, type=Path, help="Customer fixture JSON path")
    parser.add_argument("--corpus", required=True, type=Path, help="Pre-linked corpus JSON path")
    parser.add_argument("--catalog", required=True, type=Path, help="Bootstrap stance catalog path")
    parser.add_argument("--events", type=Path, default=None, help="Optional sibling event context JSON path")
    parser.add_argument("--out", type=Path, default=None, help="Snapshot output path")
    parser.add_argument("--include-comments", action="store_true", help="Include comments in claim extraction")
    args = parser.parse_args()

    customer = load_customer(args.customer)
    llm = default_cached_llm()
    state = StreamingState(
        stance_catalog=load_bootstrap(args.catalog),
        claim_catalogs=ClaimCatalogStore(),
    )
    pipeline = StreamingTagsPipeline(
        state=state,
        type_triage=TypeTriageStep(customer, llm),
        stance_tagger=StanceTagger(customer, llm),
        stance_updater=StanceUpdater(customer),
        claim_tagger=ClaimTagger(customer, llm, include_comments=args.include_comments),
        claim_updater=ClaimUpdater(customer, llm),
    )
    retriever = LinkedJsonRetriever(args.corpus, args.events)
    for bundle in retriever.iter_bundles(customer):
        pipeline.process_bundle(bundle)
        customer.items_processed_total += len(bundle.items)
        customer.items_processed_since_last_pass += len(bundle.items)
    out = args.out or Path("data") / "tags" / customer.slug / f"run_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
    save_snapshot(out, stance_catalog=state.stance_catalog, claim_catalogs=state.claim_catalogs)
    print(f"wrote {out}")


if __name__ == "__main__":
    main()

