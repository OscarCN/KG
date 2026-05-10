"""Periodic stance catalog consistency pass."""

from __future__ import annotations

import os
from collections import defaultdict
from typing import Any

from src.entities.tags_gpt.catalogs import ClaimCatalogStore, StanceCatalog
from src.entities.tags_gpt.llm import JsonLlm
from src.entities.tags_gpt.models import (
    ConsistencyPassResult,
    Customer,
    STANCE_BEARING_TYPES,
    SourceItem,
    StanceDecision,
    StanceProposal,
    StanceType,
    StepSummary,
    now_iso,
)
from src.entities.tags_gpt.prompts import compact_customer, consistency_prompt


class ConsistencyPassStep:
    def __init__(
        self,
        customer: Customer,
        llm: JsonLlm,
        *,
        model: str | None = None,
        sample_size: int = 300,
    ):
        self.customer = customer
        self.llm = llm
        self.model = model or os.environ.get("OPENROUTER_CONSISTENCY_MODEL", "openai/gpt-4o")
        self.sample_size = sample_size

    def run(
        self,
        catalog: StanceCatalog,
        *,
        items_seen: dict[str, SourceItem] | None = None,
        claim_catalogs: ClaimCatalogStore | None = None,
    ) -> ConsistencyPassResult:
        started = now_iso()
        summary = StepSummary("consistency_pass")
        sample = self._sample_assignments(catalog)
        by_type: dict[StanceType, list[dict[str, Any]]] = defaultdict(list)
        for assignment in sample:
            if assignment.stance_type in STANCE_BEARING_TYPES:
                by_type[assignment.stance_type].append(assignment.to_dict())
        result = ConsistencyPassResult(
            customer_id=self.customer.entity_id,
            started_at=started,
            finished_at=started,
            sample_size=len(sample),
            sample_strategy={"max": self.sample_size, "oversample_uncatalogued": True},
            summary=summary,
        )
        for stance_type, rows in by_type.items():
            payload = {
                "customer": compact_customer(self.customer),
                "stance_type": stance_type,
                "entries": [
                    entry.to_dict()
                    for entry in catalog.iter_entries({stance_type})
                ],
                "assignments": rows,
                "source_items": [
                    items_seen[item_id].to_dict()
                    for item_id in {row["source_item_id"] for row in rows}
                    if items_seen and item_id in items_seen
                ],
            }
            response = self.llm.complete_json(
                phase="consistency_pass",
                payload=payload,
                prompt=consistency_prompt(payload, stance_type),
                model=self.model,
            )
            self._apply_response(catalog, result, response, stance_type)
        result.finished_at = now_iso()
        return result

    def _sample_assignments(self, catalog: StanceCatalog):
        uncatalogued = [a for a in catalog.assignments if a.stance_id is None]
        catalogued = [a for a in catalog.assignments if a.stance_id is not None]
        return (uncatalogued + catalogued)[: self.sample_size]

    def _apply_response(
        self,
        catalog: StanceCatalog,
        result: ConsistencyPassResult,
        response: dict[str, Any],
        stance_type: StanceType,
    ) -> None:
        for raw in response.get("proposals") or []:
            if not isinstance(raw, dict):
                continue
            proposal = StanceProposal(
                kind=raw.get("kind", "add"),
                label=str(raw.get("label") or ""),
                description=str(raw.get("description") or ""),
                stance_type=stance_type,
                source_item_ids=[str(x) for x in raw.get("source_item_ids") or []],
                src_stance_id=raw.get("src_stance_id"),
            )
            result.proposals.append(proposal)
            if proposal.kind == "add" and proposal.label:
                catalog.add(proposal.label, proposal.description, primary_type=stance_type)
                result.decisions.append(StanceDecision(len(result.proposals) - 1, "accept", reason="consistency"))
                result.summary.inc("add")
            elif proposal.kind == "rename" and proposal.src_stance_id and catalog.rename(proposal.src_stance_id, proposal.label, proposal.description):
                result.decisions.append(StanceDecision(len(result.proposals) - 1, "rename", existing_id=proposal.src_stance_id, reason="consistency"))
                result.summary.inc("rename")
            else:
                result.decisions.append(StanceDecision(len(result.proposals) - 1, "reject", reason="validation_failed"))
        for src_id, dst_id in response.get("merge_pairs") or []:
            src = catalog.entries.get(src_id)
            dst = catalog.entries.get(dst_id)
            if src and dst and src.primary_type == dst.primary_type == stance_type and catalog.merge(src_id, dst_id):
                result.merge_pairs.append((src_id, dst_id))
                result.summary.inc("merge")
        for stance_id in response.get("retire_ids") or []:
            entry = catalog.entries.get(stance_id)
            if entry and entry.primary_type == stance_type and catalog.retire(stance_id):
                result.retire_ids.append(stance_id)
                result.summary.inc("retire")
        for from_id, to_id in response.get("reroute_pairs") or []:
            if catalog.reroute(from_id, to_id):
                result.reroute_pairs.append((from_id, to_id))
                result.summary.inc("reroute")

