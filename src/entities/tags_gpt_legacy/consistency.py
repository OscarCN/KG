"""Periodic typed stance catalog consistency pass."""

from __future__ import annotations

import os
from collections import defaultdict
from typing import Optional

from src.entities.tags_gpt.catalogs import ClaimCatalogStore, StanceCatalog
from src.entities.tags_gpt.llm import JsonLlm
from src.entities.tags_gpt.models import (
    ConsistencyPassResult,
    Customer,
    SourceItem,
    STANCE_BEARING_TYPES,
    TAG_ONLY_TYPES,
    StanceAssignment,
    StanceProposal,
    StanceTagging,
    StanceType,
    StepSummary,
    now_iso,
)
from src.entities.tags_gpt.prompts import consistency_pass_prompt
from src.entities.tags_gpt.tagging import StanceUpdater


class ConsistencyPassStep:
    def __init__(
        self,
        customer: Customer,
        llm: JsonLlm,
        stance_updater: Optional[StanceUpdater] = None,
        *,
        model: Optional[str] = None,
        sample_size: int = 300,
    ):
        self.customer = customer
        self.llm = llm
        self.stance_updater = stance_updater or StanceUpdater(customer, llm)
        self.model = model or os.environ.get("OPENROUTER_CONSISTENCY_MODEL", "openai/gpt-4o")
        self.sample_size = sample_size

    def run(
        self,
        catalog: StanceCatalog,
        *,
        items_seen: dict[str, SourceItem],
        claim_catalogs: Optional[ClaimCatalogStore] = None,
        sample: Optional[list[StanceAssignment]] = None,
    ) -> ConsistencyPassResult:
        started_at = now_iso()
        sampled = sample or self._sample(catalog.assignments)
        assignment_samples, item_samples, local_item_by_id = self._compact_samples(sampled, items_seen)
        claim_summaries = self._claim_summaries(claim_catalogs)
        response = self.llm.complete_json(
            phase="stance_consistency",
            payload={
                "customer": {
                    "name": self.customer.name,
                    "description": self.customer.description,
                },
                "catalog": catalog.snapshot(),
                "assignments": assignment_samples,
                "items": item_samples,
                "claims": claim_summaries,
            },
            prompt=consistency_pass_prompt(
                self.customer,
                catalog.snapshot(),
                assignment_samples,
                item_samples,
                claim_summaries,
            ),
            model=self.model,
        )

        proposals = self._parse_proposals(response.get("proposals") or [], local_item_by_id)
        tagging = StanceTagging(proposals=proposals)
        sampled_item_ids = {assignment.source_item_id for assignment in sampled}
        summary = self.stance_updater.update(
            catalog,
            tagging,
            sample_items=[item for item_id, item in items_seen.items() if item_id in sampled_item_ids],
            allow_all_growth=True,
        )

        merge_pairs = self._parse_pairs(response.get("merge_pairs") or [], "src_id", "dst_id")
        for src_id, dst_id in merge_pairs:
            if catalog.merge(src_id, dst_id):
                summary.inc("merged")

        retire_ids = self._parse_ids(response.get("retire_ids") or [])
        for stance_id in retire_ids:
            if catalog.retire(stance_id):
                summary.inc("retired")

        reroute_pairs = self._parse_pairs(response.get("reroute_pairs") or [], "from_id", "to_id")
        for from_id, to_id in reroute_pairs:
            count = catalog.reroute(from_id, to_id)
            if count:
                summary.inc("rerouted", count)

        for assignment in sampled:
            assignment.consistency_used = True

        return ConsistencyPassResult(
            customer_id=self.customer.entity_id,
            started_at=started_at,
            finished_at=now_iso(),
            sample_size=len(sampled),
            sample_strategy={"strategy": "stratified_by_type", "max_items": self.sample_size},
            proposals=proposals,
            merge_pairs=merge_pairs,
            retire_ids=retire_ids,
            reroute_pairs=reroute_pairs,
            decisions=[],
            summary=summary,
        )

    def _sample(self, assignments: list[StanceAssignment]) -> list[StanceAssignment]:
        buckets: dict[str, list[StanceAssignment]] = defaultdict(list)
        for assignment in reversed(assignments):
            buckets[assignment.stance_type].append(assignment)
        sampled: list[StanceAssignment] = []
        while len(sampled) < self.sample_size and any(buckets.values()):
            for stance_type in sorted(buckets):
                bucket = buckets[stance_type]
                if bucket:
                    sampled.append(bucket.pop(0))
                    if len(sampled) >= self.sample_size:
                        break
        return sampled

    @staticmethod
    def _compact_samples(
        assignments: list[StanceAssignment],
        items_seen: dict[str, SourceItem],
    ) -> tuple[list[dict], list[dict], dict[str, SourceItem]]:
        item_ids = []
        for assignment in assignments:
            if assignment.source_item_id not in item_ids:
                item_ids.append(assignment.source_item_id)
        item_samples: list[dict] = []
        local_item_by_id: dict[str, SourceItem] = {}
        local_id_by_source_id: dict[str, str] = {}
        for source_item_id in item_ids:
            item = items_seen.get(source_item_id)
            if item is None:
                continue
            local_id = str(len(item_samples) + 1)
            local_item_by_id[local_id] = item
            local_id_by_source_id[item.id] = local_id
            item_samples.append(
                {
                    "id": int(local_id),
                    "kind": item.kind,
                    "text": item.short_text(700),
                }
            )

        assignment_samples = [
            {
                "source_item_id": (
                    int(local_id)
                    if (local_id := local_id_by_source_id.get(assignment.source_item_id)) is not None
                    else None
                ),
                "source_kind": assignment.source_kind,
                "stance_type": assignment.stance_type,
                "stance_id": assignment.stance_id,
                "sentiment": assignment.sentiment,
                "consistency_relevance": assignment.consistency_relevance,
                "reason": assignment.reason,
            }
            for assignment in assignments
        ]
        return assignment_samples, item_samples, local_item_by_id

    @staticmethod
    def _claim_summaries(claim_catalogs: Optional[ClaimCatalogStore]) -> list[dict]:
        if claim_catalogs is None:
            return []
        out: list[dict] = []
        for catalog in claim_catalogs.values():
            for cluster in catalog.clusters.values():
                out.append(
                    {
                        "canonical": cluster.canonical,
                        "n_members": len(cluster.members),
                        "importance_max": cluster.importance_max,
                    }
                )
        return out

    @staticmethod
    def _parse_proposals(
        rows: list,
        local_item_by_id: dict[str, SourceItem],
    ) -> list[StanceProposal]:
        proposals: list[StanceProposal] = []
        for raw in rows:
            if not isinstance(raw, dict):
                continue
            kind = raw.get("kind")
            stance_type = raw.get("stance_type")
            label = str(raw.get("label") or "").strip()
            if (
                kind not in ("add", "rename")
                or stance_type not in STANCE_BEARING_TYPES
                or stance_type in TAG_ONLY_TYPES
                or not label
            ):
                continue
            proposals.append(
                StanceProposal(
                    kind=kind,
                    label=label,
                    description=str(raw.get("description") or "").strip(),
                    stance_type=stance_type,
                    source_item_ids=[
                        item.id
                        for value in raw.get("source_item_ids") or []
                        if (item := local_item_by_id.get(_local_id(value))) is not None
                    ],
                    src_stance_id=raw.get("src_stance_id"),
                )
            )
        return proposals

    @staticmethod
    def _parse_pairs(rows: list, src_key: str, dst_key: str) -> list[tuple[str, str]]:
        pairs: list[tuple[str, str]] = []
        for raw in rows:
            if not isinstance(raw, dict):
                continue
            src = str(raw.get(src_key) or "").strip()
            dst = str(raw.get(dst_key) or "").strip()
            if src and dst and src != dst:
                pairs.append((src, dst))
        return pairs

    @staticmethod
    def _parse_ids(rows: list) -> list[str]:
        out: list[str] = []
        for raw in rows:
            stance_id = str(raw.get("stance_id") if isinstance(raw, dict) else raw or "").strip()
            if stance_id:
                out.append(stance_id)
        return out


def _local_id(value) -> str:
    if value is None:
        return ""
    return str(value).strip()
