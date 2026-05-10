"""Bootstrap the customer stance catalog from a broad corpus."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any, Optional

from src.entities.tags_gpt.catalogs import StanceCatalog
from src.entities.tags_gpt.llm import JsonLlm
from src.entities.tags_gpt.models import (
    Customer,
    LinkedEvent,
    STANCE_BEARING_TYPES,
    SourceItem,
    StanceType,
    TypeTriageResult,
)
from src.entities.tags_gpt.prompts import bootstrap_catalog_prompt
from src.entities.tags_gpt.tagging import TypeTriageStep


BOOTSTRAP_CATALOG_TYPES: tuple[StanceType, ...] = (
    "entity_stance",
    "complaint",
    "gratefulness",
    "suggestion",
    "denuncia",
    "endorsement",
    "question",
)
MIN_BOOTSTRAP_EVIDENCE = 2
BOOTSTRAP_TRIAGE_BATCH_SIZE = 12
MAX_BOOTSTRAP_ENTRIES: dict[StanceType, int] = {
    "entity_stance": 15,
    "complaint": 10,
    "gratefulness": 10,
    "suggestion": 10,
    "denuncia": 10,
    "endorsement": 10,
    "question": 10,
}


@dataclass
class BootstrapTriageCall:
    batch_index: int
    payload: dict[str, Any] = field(default_factory=dict)
    prompt: str = ""
    response: dict[str, Any] = field(default_factory=dict)
    result: TypeTriageResult = field(default_factory=TypeTriageResult)


@dataclass
class BootstrapTriageResult:
    items_by_type: dict[StanceType, list[SourceItem]] = field(default_factory=dict)
    calls: list[BootstrapTriageCall] = field(default_factory=list)
    dropped_invalid: int = 0
    dropped_tag_only: int = 0
    dropped_duplicate: int = 0


@dataclass
class BootstrapCatalogResult:
    stance_type: StanceType
    created: int = 0
    payload: dict[str, Any] = field(default_factory=dict)
    prompt: str = ""
    response: dict[str, Any] = field(default_factory=dict)
    dropped_invalid: int = 0
    dropped_insufficient_evidence: int = 0
    skipped: bool = False


@dataclass
class BootstrapRunResult:
    catalog: StanceCatalog
    items: list[SourceItem] = field(default_factory=list)
    triage: Optional[BootstrapTriageResult] = None
    catalog_results: list[BootstrapCatalogResult] = field(default_factory=list)


class StanceBootstrapStep:
    def __init__(
        self,
        llm: JsonLlm,
        *,
        model: Optional[str] = None,
        triage_model: Optional[str] = None,
        max_items: int = 200,
        min_evidence: int = MIN_BOOTSTRAP_EVIDENCE,
        triage_batch_size: int = BOOTSTRAP_TRIAGE_BATCH_SIZE,
    ):
        self.llm = llm
        self.model = model or os.environ.get("OPENROUTER_BOOTSTRAP_MODEL", "openai/gpt-4o")
        self.triage_model = triage_model
        self.max_items = max_items
        self.min_evidence = min_evidence
        self.triage_batch_size = triage_batch_size

    def bootstrap(self, customer: Customer, corpus: list[SourceItem]) -> StanceCatalog:
        return self.bootstrap_with_debug(customer, corpus).catalog

    def bootstrap_with_debug(
        self,
        customer: Customer,
        corpus: list[SourceItem],
    ) -> BootstrapRunResult:
        catalog = StanceCatalog(customer.entity_id)
        items = corpus[: self.max_items]
        if not items:
            return BootstrapRunResult(catalog=catalog, items=[])

        triage = self.triage_corpus(customer, items)
        catalog_results: list[BootstrapCatalogResult] = []
        for stance_type in BOOTSTRAP_CATALOG_TYPES:
            result = self.bootstrap_catalog_type(
                customer,
                catalog,
                stance_type,
                triage.items_by_type.get(stance_type, []),
            )
            catalog_results.append(result)

        return BootstrapRunResult(
            catalog=catalog,
            items=items,
            triage=triage,
            catalog_results=catalog_results,
        )

    def triage_corpus(
        self,
        customer: Customer,
        items: list[SourceItem],
    ) -> BootstrapTriageResult:
        result = BootstrapTriageResult()
        triage_step = TypeTriageStep(customer, self.llm, model=self.triage_model)
        event = _bootstrap_event(customer)
        triage_debug = triage_step.triage_with_debug(
            event,
            items,
            batch_size=self.triage_batch_size,
        )
        result.calls = [
            BootstrapTriageCall(
                batch_index=call.batch_index,
                payload=call.payload,
                prompt=call.prompt,
                response=call.response,
                result=call.result,
            )
            for call in triage_debug.calls
        ]
        result.dropped_invalid = triage_debug.result.dropped_invalid
        seen: set[tuple[str, StanceType]] = set()
        items_by_id = {item.id: item for item in items}
        for hint in triage_debug.result.triaged:
            if hint.stance_type not in STANCE_BEARING_TYPES:
                result.dropped_tag_only += 1
                continue
            key = (hint.source_item_id, hint.stance_type)
            if key in seen:
                result.dropped_duplicate += 1
                continue
            item = items_by_id.get(hint.source_item_id)
            if item is None:
                result.dropped_invalid += 1
                continue
            seen.add(key)
            result.items_by_type.setdefault(hint.stance_type, []).append(item)
        return result

    def bootstrap_catalog_type(
        self,
        customer: Customer,
        catalog: StanceCatalog,
        stance_type: StanceType,
        items: list[SourceItem],
    ) -> BootstrapCatalogResult:
        if stance_type not in BOOTSTRAP_CATALOG_TYPES:
            return BootstrapCatalogResult(stance_type=stance_type, skipped=True)
        if not items:
            return BootstrapCatalogResult(stance_type=stance_type, skipped=True)

        local_items, local_item_by_id = _local_items(items)
        payload = {
            "customer": _customer_payload(customer),
            "stance_type": stance_type,
            "min_evidence": self.min_evidence,
            "items": local_items,
        }
        prompt = bootstrap_catalog_prompt(
            customer,
            stance_type,
            local_items,
            min_evidence=self.min_evidence,
        )
        response = self.llm.complete_json(
            phase="stance_bootstrap_catalog",
            payload=payload,
            prompt=prompt,
            model=self.model,
        )

        result = BootstrapCatalogResult(
            stance_type=stance_type,
            payload=payload,
            prompt=prompt,
            response=response,
        )
        for raw in response.get("entries") or response.get("stances") or []:
            if result.created >= MAX_BOOTSTRAP_ENTRIES[stance_type]:
                break
            if not isinstance(raw, dict):
                result.dropped_invalid += 1
                continue
            label = str(raw.get("label") or "").strip()
            if not label:
                result.dropped_invalid += 1
                continue
            evidence = _evidence_items(raw.get("evidence_source_item_ids") or [], local_item_by_id)
            if len(evidence) < self.min_evidence:
                result.dropped_insufficient_evidence += 1
                continue
            catalog.add(
                label,
                str(raw.get("description") or "").strip(),
                primary_type=stance_type,
            )
            result.created += 1
        return result


def _customer_payload(customer: Customer) -> dict[str, Any]:
    return {
        "name": customer.name,
        "description": customer.description,
    }


def _local_items(
    items: list[SourceItem],
    *,
    text_limit: int = 700,
) -> tuple[list[dict[str, Any]], dict[str, SourceItem]]:
    payload: list[dict[str, Any]] = []
    local_item_by_id: dict[str, SourceItem] = {}
    for index, item in enumerate(items, start=1):
        local_id = str(index)
        local_item_by_id[local_id] = item
        payload.append(
            {
                "id": index,
                "text": item.short_text(text_limit),
            }
        )
    return payload, local_item_by_id


def _bootstrap_event(customer: Customer) -> LinkedEvent:
    description = (
        f"Corpus general para construir catálogos iniciales de posturas "
        f"sobre {customer.name}. {customer.description}"
    ).strip()
    return LinkedEvent(
        id="bootstrap",
        event_type="bootstrap",
        name=f"Bootstrap {customer.name}",
        description=description,
    )


def _evidence_items(
    values: list[Any],
    local_item_by_id: dict[str, SourceItem],
) -> list[SourceItem]:
    out: list[SourceItem] = []
    seen: set[str] = set()
    for value in values:
        item = local_item_by_id.get(_local_id(value))
        if item is None or item.id in seen:
            continue
        seen.add(item.id)
        out.append(item)
    return out


def _local_id(value) -> str:
    if value is None:
        return ""
    return str(value).strip()
