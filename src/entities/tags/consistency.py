"""Periodic consistency pass (design §5.7).

Steps:
    1. Stratified sample of recent assignments by `stance_type`. Oversample
       `stance_id is None` rows (they're the main growth signal).
    2. Group sample by `stance_type` (no re-triage — type already on each row).
    3. Per active type, ONE consolidation LLM call → proposals + merge_pairs
       + retire_ids + reroute_pairs.
    4. Validate, then apply directly via `StanceCatalog` methods (no
       adjudicator LLM — mirrors §5.4).
"""

from __future__ import annotations

import logging
import random
from typing import Optional

from src.entities.tags.catalogs import ClaimCatalogStore, StanceCatalog
from src.entities.tags.llm import JsonLlm
from src.entities.tags.models import (
    ConsistencyPassResult,
    Customer,
    SourceItem,
    StanceAssignment,
    StanceProposal,
    StanceType,
    StepSummary,
    now_iso,
)
from src.entities.tags.prompts import consistency_prompt_for_type
from src.entities.tags.streaming import STANCE_BEARING_ACTIVE_TYPES


logger = logging.getLogger(__name__)


DEFAULT_SAMPLE_SIZE = 300
DEFAULT_NULL_OVERSAMPLE_FACTOR = 2.0
DEFAULT_ITEM_SAMPLES_PER_CALL = 30
DEFAULT_ASSIGNMENTS_PER_CALL = 80


class ConsistencyPassStep:
    def __init__(
        self,
        customer: Customer,
        llm: JsonLlm,
        *,
        sample_size: int = DEFAULT_SAMPLE_SIZE,
        null_oversample_factor: float = DEFAULT_NULL_OVERSAMPLE_FACTOR,
        rng_seed: Optional[int] = None,
    ):
        self.customer = customer
        self.llm = llm
        self.sample_size = sample_size
        self.null_oversample_factor = null_oversample_factor
        self.rng = random.Random(rng_seed)

    def run(
        self,
        catalog: StanceCatalog,
        items_seen: dict[str, SourceItem],
        claim_catalogs: Optional[ClaimCatalogStore] = None,
    ) -> ConsistencyPassResult:
        result = ConsistencyPassResult(customer_id=self.customer.entity_id)
        result.started_at = now_iso()
        summary = StepSummary(name="consistency_pass")

        # 1. Sample stratified by stance_type.
        sample = self._stratified_sample(catalog.assignments)
        result.sample_size = len(sample)
        result.sample_strategy = self._sample_strategy_summary(sample)

        # 2. Group by stance_type.
        per_type: dict[StanceType, list[StanceAssignment]] = {}
        for a in sample:
            per_type.setdefault(a.stance_type, []).append(a)

        # 3 + 4. Per active type, run consolidation, validate, apply.
        claim_summaries = self._claim_summaries(claim_catalogs)
        for stance_type in STANCE_BEARING_ACTIVE_TYPES:
            assignments_for_type = per_type.get(stance_type) or []
            if not assignments_for_type:
                continue
            self._run_one_type(
                catalog,
                stance_type,
                assignments_for_type,
                items_seen,
                claim_summaries,
                result,
                summary,
            )

        # 5. Update customer counters.
        self.customer.last_consistency_pass_at = now_iso()
        self.customer.last_consistency_pass_count = result.sample_size
        self.customer.items_processed_since_last_pass = 0

        result.finished_at = now_iso()
        result.summary = summary
        return result

    # ── per-type runner ────────────────────────────────────────────────

    def _run_one_type(
        self,
        catalog: StanceCatalog,
        stance_type: StanceType,
        assignments: list[StanceAssignment],
        items_seen: dict[str, SourceItem],
        claim_summaries: list[dict],
        result: ConsistencyPassResult,
        summary: StepSummary,
    ) -> None:
        catalog_slice = catalog.snapshot(types=[stance_type])
        assignment_payload = [
            {
                "source_item_id": a.source_item_id,
                "stance_id": a.stance_id,
                "event_id": a.event_id,
                "reason": a.reason,
            }
            for a in assignments[:DEFAULT_ASSIGNMENTS_PER_CALL]
        ]
        # Pull the source items behind those assignments.
        sample_item_ids = list({a.source_item_id for a in assignments})[
            :DEFAULT_ITEM_SAMPLES_PER_CALL
        ]
        item_payload = []
        for sid in sample_item_ids:
            item = items_seen.get(sid)
            if item is None:
                continue
            item_payload.append(
                {"id": item.id, "kind": item.kind, "text": item.short_text(800)}
            )

        prompt = consistency_prompt_for_type(
            self.customer,
            stance_type,
            catalog_slice,
            assignment_payload,
            item_payload,
            claim_summaries,
        )
        response = self.llm.call(prompt)
        if not isinstance(response, dict):
            logger.warning("consistency[%s]: malformed response", stance_type)
            return

        existing_ids = {entry["id"] for entry in catalog_slice}

        # Proposals (add / rename) — apply via StanceUpdater-style logic
        # but inline since this step owns the mutations.
        for raw in response.get("proposals") or []:
            if not isinstance(raw, dict):
                continue
            kind = raw.get("kind")
            label = str(raw.get("label") or "").strip()
            description = str(raw.get("description") or "")
            if not label:
                summary.inc(f"{stance_type}_proposal_dropped_empty_label")
                continue
            proposal = StanceProposal(
                kind=kind if kind in {"add", "rename"} else "add",  # type: ignore[arg-type]
                label=label,
                description=description,
                stance_type=stance_type,
                source_item_ids=[
                    str(x) for x in (raw.get("source_item_ids") or [])
                ],
                src_stance_id=raw.get("src_stance_id"),
            )
            result.proposals.append(proposal)
            if proposal.kind == "add":
                norm = proposal.label.strip().lower()
                existing_match = next(
                    (
                        e
                        for e in catalog.iter_entries(types=[stance_type])
                        if e.label.strip().lower() == norm
                    ),
                    None,
                )
                if existing_match is not None:
                    summary.inc(f"{stance_type}_proposal_add_already_exists")
                    continue
                entry = catalog.add(
                    label=proposal.label,
                    description=proposal.description,
                    primary_type=stance_type,
                )
                existing_ids.add(entry.id)
                summary.inc(f"{stance_type}_proposal_add_accepted")
            else:  # rename
                src = proposal.src_stance_id
                if src and src in existing_ids and catalog.rename(src, proposal.label, description):
                    summary.inc(f"{stance_type}_proposal_rename_accepted")
                else:
                    summary.inc(f"{stance_type}_proposal_rename_dropped")

        # merge_pairs (intra-type)
        for raw in response.get("merge_pairs") or []:
            if not isinstance(raw, dict):
                continue
            src_id = raw.get("src_id")
            dst_id = raw.get("dst_id")
            if (
                src_id and dst_id
                and src_id in existing_ids
                and dst_id in existing_ids
                and src_id != dst_id
            ):
                src_entry = catalog.entries.get(src_id)
                dst_entry = catalog.entries.get(dst_id)
                if src_entry and dst_entry and src_entry.primary_type != dst_entry.primary_type:
                    summary.inc(f"{stance_type}_merge_dropped_cross_type")
                    continue
                moved = catalog.merge(src_id, dst_id)
                if moved or src_id not in catalog.entries:
                    result.merge_pairs.append((src_id, dst_id))
                    existing_ids.discard(src_id)
                    summary.inc(f"{stance_type}_merge_applied")
            else:
                summary.inc(f"{stance_type}_merge_dropped_invalid")

        # retire_ids
        for raw in response.get("retire_ids") or []:
            if not isinstance(raw, dict):
                continue
            sid = raw.get("stance_id")
            if sid and sid in existing_ids and catalog.retire(sid):
                result.retire_ids.append(sid)
                existing_ids.discard(sid)
                summary.inc(f"{stance_type}_retire_applied")
            else:
                summary.inc(f"{stance_type}_retire_dropped_invalid")

        # reroute_pairs
        for raw in response.get("reroute_pairs") or []:
            if not isinstance(raw, dict):
                continue
            from_id = raw.get("from_id")
            to_id = raw.get("to_id")
            if from_id and to_id and from_id != to_id:
                moved = catalog.reroute(from_id, to_id)
                if moved:
                    result.reroute_pairs.append((from_id, to_id))
                    summary.inc(f"{stance_type}_reroute_applied")
                else:
                    summary.inc(f"{stance_type}_reroute_no_change")
            else:
                summary.inc(f"{stance_type}_reroute_dropped_invalid")

    # ── helpers ────────────────────────────────────────────────────────

    def _stratified_sample(
        self, assignments: list[StanceAssignment]
    ) -> list[StanceAssignment]:
        """Sample stratified by stance_type, oversampling null-stance rows."""
        if not assignments:
            return []
        if len(assignments) <= self.sample_size:
            return list(assignments)

        by_type: dict[StanceType, list[StanceAssignment]] = {}
        for a in assignments:
            by_type.setdefault(a.stance_type, []).append(a)

        types = [t for t in by_type if by_type[t]]
        per_type_quota = max(1, self.sample_size // max(1, len(types)))

        out: list[StanceAssignment] = []
        for t, rows in by_type.items():
            null_rows = [r for r in rows if r.stance_id is None]
            cat_rows = [r for r in rows if r.stance_id is not None]

            null_quota = min(
                len(null_rows),
                int(per_type_quota * self.null_oversample_factor / (1 + self.null_oversample_factor) + 0.5),
            )
            cat_quota = min(len(cat_rows), per_type_quota - null_quota)

            self.rng.shuffle(null_rows)
            self.rng.shuffle(cat_rows)
            out.extend(null_rows[:null_quota])
            out.extend(cat_rows[:cat_quota])

        # If we under-quota'd, top up randomly from the remainder.
        if len(out) < self.sample_size:
            picked_ids = set(id(a) for a in out)
            remainder = [a for a in assignments if id(a) not in picked_ids]
            self.rng.shuffle(remainder)
            out.extend(remainder[: self.sample_size - len(out)])
        return out[: self.sample_size]

    @staticmethod
    def _sample_strategy_summary(sample: list[StanceAssignment]) -> dict:
        by_type: dict[str, dict[str, int]] = {}
        for a in sample:
            t = a.stance_type
            row = by_type.setdefault(t, {"total": 0, "null": 0})
            row["total"] += 1
            if a.stance_id is None:
                row["null"] += 1
        return {"by_type": by_type, "total": len(sample)}

    @staticmethod
    def _claim_summaries(claim_catalogs: Optional[ClaimCatalogStore]) -> list[dict]:
        if claim_catalogs is None:
            return []
        out = []
        for (cust, ev_id), cat in claim_catalogs.catalogs.items():
            top = []
            for canonical, n, importance_max, is_new in cat.summary()[:5]:
                top.append({"canonical": canonical, "n": n, "importance_max": importance_max})
            out.append(
                {
                    "event_id": ev_id,
                    "n_clusters": len(cat.clusters),
                    "top": top,
                }
            )
        return out
