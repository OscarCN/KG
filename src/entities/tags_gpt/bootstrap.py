"""Bootstrap typed stance catalogs from a local customer corpus."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any

from src.entities.tags_gpt.catalogs import StanceCatalog
from src.entities.tags_gpt.llm import JsonLlm
from src.entities.tags_gpt.models import (
    ArticleBundle,
    Customer,
    STANCE_BEARING_TYPES,
    SourceItem,
    StanceType,
    TypeTriageItem,
)
from src.entities.tags_gpt.prompts import bootstrap_catalog_prompt
from src.entities.tags_gpt.tagging import TypeTriageStep, _local_items


@dataclass
class BootstrapCatalogResult:
    stance_type: StanceType
    created: int = 0
    dropped_invalid: int = 0
    dropped_insufficient_evidence: int = 0
    skipped: bool = False
    payload: dict[str, Any] = field(default_factory=dict)
    response: dict[str, Any] = field(default_factory=dict)


@dataclass
class BootstrapRunResult:
    catalog: StanceCatalog
    catalog_results: list[BootstrapCatalogResult] = field(default_factory=list)


class StanceBootstrapStep:
    def __init__(
        self,
        llm: JsonLlm,
        *,
        model: str | None = None,
        triage_model: str | None = None,
        min_evidence: int = 2,
        max_items: int = 200,
        max_entries_per_type: int = 15,
    ):
        self.llm = llm
        self.model = model or os.environ.get("OPENROUTER_BOOTSTRAP_MODEL", "openai/gpt-4o")
        self.triage_model = triage_model
        self.min_evidence = min_evidence
        self.max_items = max_items
        self.max_entries_per_type = max_entries_per_type

    def bootstrap(self, customer: Customer, corpus: list[SourceItem]) -> StanceCatalog:
        return self.bootstrap_with_result(customer, corpus).catalog

    def bootstrap_with_result(self, customer: Customer, corpus: list[SourceItem]) -> BootstrapRunResult:
        catalog = StanceCatalog(customer.entity_id)
        items = corpus[: self.max_items]
        triage = self._triage_corpus(customer, items)
        by_type: dict[StanceType, list[SourceItem]] = {}
        items_by_id = {item.id: item for item in items}
        for hint in triage:
            if hint.stance_type not in STANCE_BEARING_TYPES:
                continue
            item = items_by_id.get(hint.source_item_id)
            if item:
                by_type.setdefault(hint.stance_type, []).append(item)
        results = [
            self._bootstrap_type(customer, catalog, stance_type, by_type.get(stance_type, []))
            for stance_type in STANCE_BEARING_TYPES
        ]
        return BootstrapRunResult(catalog=catalog, catalog_results=results)

    def _triage_corpus(self, customer: Customer, items: list[SourceItem]) -> list[TypeTriageItem]:
        triage_step = TypeTriageStep(customer, self.llm, model=self.triage_model)
        bundle = ArticleBundle(root=items[0], comments=items[1:], customer=customer) if items else None
        if bundle is None:
            return []
        return triage_step.triage(bundle).triaged

    def _bootstrap_type(
        self,
        customer: Customer,
        catalog: StanceCatalog,
        stance_type: StanceType,
        items: list[SourceItem],
    ) -> BootstrapCatalogResult:
        if not items:
            return BootstrapCatalogResult(stance_type=stance_type, skipped=True)
        local_items, by_local = _local_items(items, limit=700)
        payload = {
            "customer": {"name": customer.name, "description": customer.description},
            "stance_type": stance_type,
            "min_evidence": self.min_evidence,
            "items": local_items,
        }
        response = self.llm.complete_json(
            phase="stance_bootstrap_catalog",
            payload=payload,
            prompt=bootstrap_catalog_prompt(customer, stance_type, local_items, min_evidence=self.min_evidence),
            model=self.model,
        )
        result = BootstrapCatalogResult(stance_type=stance_type, payload=payload, response=response)
        for raw in response.get("entries") or response.get("stances") or []:
            if result.created >= self.max_entries_per_type:
                break
            if not isinstance(raw, dict):
                result.dropped_invalid += 1
                continue
            label = str(raw.get("label") or "").strip()
            evidence = [by_local.get(int(x)) for x in raw.get("evidence_source_item_ids") or [] if str(x).isdigit()]
            evidence = [x for x in evidence if x is not None]
            if not label:
                result.dropped_invalid += 1
                continue
            if len(evidence) < self.min_evidence:
                result.dropped_insufficient_evidence += 1
                continue
            catalog.add(label, str(raw.get("description") or ""), primary_type=stance_type)
            result.created += 1
        return result

