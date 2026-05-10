"""Phase 1 — bootstrap a per-customer stance catalog from a corpus.

Single LLM call. The corpus is articles + posts + comments tied to the
customer's content graph (already filtered upstream by the linker).
"""

from __future__ import annotations

import logging
import os
from typing import Iterable

from src.entities.tags._llm_io import (
    call_cached,
    customer_context_block,
    load_prompt,
    render_prompt,
)
from src.entities.tags.models.customer import Customer
from src.entities.tags.models.source_item import SourceItem
from src.entities.tags.models.stance_catalog import StanceCatalog, StanceEntry

logger = logging.getLogger(__name__)


_DEFAULT_MODEL = os.environ.get("OPENROUTER_BOOTSTRAP_MODEL", "openai/gpt-4o")
_PHASE = "bootstrap"
_MAX_CORPUS_ITEMS = 200


def _items_block(items: Iterable[SourceItem]) -> str:
    lines = []
    for it in items:
        lines.append(f"- [{it.kind}] {it.id}: {it.text[:600]}")
    return "\n".join(lines) or "(corpus vacío)"


def bootstrap_stance_catalog(
    customer: Customer,
    corpus: list[SourceItem],
    model: str = _DEFAULT_MODEL,
    use_cache: bool = True,
) -> StanceCatalog:
    """Produce an initial StanceCatalog for the customer from `corpus`."""
    catalog = StanceCatalog(customer.entity_id)

    items = corpus[:_MAX_CORPUS_ITEMS]
    if not items:
        logger.warning("bootstrap: empty corpus, returning empty catalog")
        return catalog

    template = load_prompt("bootstrap")
    user_message = render_prompt(
        template,
        customer_context=customer_context_block(customer),
        filter_context=customer.filter_llm_prompt or "(sin filtro definido)",
        corpus_block=_items_block(items),
    )
    messages = [{"role": "user", "content": user_message}]

    payload = {
        "phase": _PHASE,
        "customer_id": customer.entity_id,
        "items": [{"id": it.id, "kind": it.kind, "text": it.text[:600]} for it in items],
    }

    parsed = call_cached(
        phase=_PHASE,
        customer_id=customer.entity_id,
        payload=payload,
        messages=messages,
        model=model,
        use_cache=use_cache,
    )
    if not parsed:
        logger.warning("bootstrap: LLM call returned no parseable response")
        return catalog

    for s in parsed.get("stances") or []:
        label = (s.get("label") or "").strip()
        description = (s.get("description") or "").strip()
        if not label:
            continue
        entry = StanceEntry.new(label, description)
        catalog.add(entry)

    logger.info(
        "bootstrap: produced %d stance entries for customer %s",
        len(catalog.entries),
        customer.entity_id,
    )
    return catalog
