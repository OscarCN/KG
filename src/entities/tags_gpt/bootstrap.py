"""Bootstrap the customer stance catalog from a broad corpus."""

from __future__ import annotations

import os
from typing import Optional

from src.entities.tags_gpt.catalogs import StanceCatalog
from src.entities.tags_gpt.llm import JsonLlm
from src.entities.tags_gpt.models import Customer, SourceItem
from src.entities.tags_gpt.prompts import bootstrap_prompt


class StanceBootstrapStep:
    def __init__(self, llm: JsonLlm, *, model: Optional[str] = None, max_items: int = 200):
        self.llm = llm
        self.model = model or os.environ.get("OPENROUTER_BOOTSTRAP_MODEL", "openai/gpt-4o")
        self.max_items = max_items

    def bootstrap(self, customer: Customer, corpus: list[SourceItem]) -> StanceCatalog:
        catalog = StanceCatalog(customer.entity_id)
        items = corpus[: self.max_items]
        if not items:
            return catalog
        payload = {
            "customer_id": customer.entity_id,
            "items": [{"id": item.id, "kind": item.kind, "text": item.short_text(700)} for item in items],
        }
        response = self.llm.complete_json(
            phase="stance_bootstrap",
            payload=payload,
            prompt=bootstrap_prompt(customer, items),
            model=self.model,
        )
        for item in response.get("stances") or []:
            label = str(item.get("label") or "").strip()
            if label:
                catalog.add(label, str(item.get("description") or "").strip())
        return catalog
