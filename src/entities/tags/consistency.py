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

from src.entities.tags.catalogs import StanceCatalog
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
        for stance_type in STANCE_BEARING_ACTIVE_TYPES:
            assignments_for_type = per_type.get(stance_type) or []
            if not assignments_for_type:
                continue
            self._run_one_type(
                catalog,
                stance_type,
                assignments_for_type,
                items_seen,
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
        result: ConsistencyPassResult,
        summary: StepSummary,
    ) -> None:
        catalog_slice = catalog.snapshot(types=[stance_type])
        existing_ids = {entry["id"] for entry in catalog_slice}

        # Build the item id-map first: int `it_N` → canonical source_item_id.
        # Then express assignments referencing those ints (and `st_N` for
        # their stance_id) so the LLM never has to echo a long string back.
        sample_item_ids = list({a.source_item_id for a in assignments})[
            :DEFAULT_ITEM_SAMPLES_PER_CALL
        ]
        item_id_map: dict[int, str] = {}
        canonical_to_short_item: dict[str, str] = {}
        item_payload: list[dict] = []
        for sid in sample_item_ids:
            item = items_seen.get(sid)
            if item is None:
                continue
            short = f"it_{len(item_payload) + 1}"
            item_id_map[len(item_payload) + 1] = item.id
            canonical_to_short_item[item.id] = short
            item_payload.append({"id": short, "text": item.short_text(800)})

        # Stance short-id map is populated by the prompt builder; we need
        # the inverse (canonical → `st_N`) for assignment_payload here, so
        # we mirror what the builder will do.
        stance_id_map: dict[str, str] = {
            f"st_{i + 1}": entry["id"] for i, entry in enumerate(catalog_slice)
        }
        canonical_to_short_stance: dict[str, str] = {
            v: k for k, v in stance_id_map.items()
        }

        assignment_payload = []
        for a in assignments[:DEFAULT_ASSIGNMENTS_PER_CALL]:
            row: dict = {
                "item_id": canonical_to_short_item.get(a.source_item_id),
                "stance_id": canonical_to_short_stance.get(a.stance_id) if a.stance_id else None,
                "reason": a.reason,
            }
            assignment_payload.append(row)

        # Per-entry assignment counts over the FULL catalog (not just the
        # sample) — gives the model the growth signal it needs to choose
        # between `split` (big n, distinguishable subideas) and `retire`
        # (small n, stale).
        counts_by_id: dict[str, int] = {}
        for a in catalog.assignments:
            if a.stance_type == stance_type and a.stance_id:
                counts_by_id[a.stance_id] = counts_by_id.get(a.stance_id, 0) + 1

        prompt = consistency_prompt_for_type(
            self.customer,
            stance_type,
            catalog_slice,
            assignment_payload,
            item_payload,
            stance_id_map=stance_id_map,
            item_id_map=item_id_map,
            counts_by_id=counts_by_id,
        )
        response = self.llm.call(prompt)
        if not isinstance(response, dict):
            logger.warning("consistency[%s]: malformed response", stance_type)
            return

        def _resolve_stance(raw_id) -> Optional[str]:
            """`st_N` (or canonical fallback) → canonical stance_id, or None."""
            if raw_id in (None, "", "null"):
                return None
            if not isinstance(raw_id, str):
                return None
            canonical = stance_id_map.get(raw_id)
            if canonical is not None:
                return canonical
            return raw_id if raw_id in existing_ids else None

        def _resolve_item(raw_id) -> Optional[str]:
            """`it_N` → canonical source_item_id; falls back to canonical
            string if it already looks like one. None otherwise."""
            if raw_id in (None, "", "null"):
                return None
            if isinstance(raw_id, str) and raw_id.startswith("it_"):
                try:
                    return item_id_map.get(int(raw_id[3:]))
                except ValueError:
                    return None
            try:
                return item_id_map.get(int(raw_id))
            except (TypeError, ValueError):
                return str(raw_id) if isinstance(raw_id, str) else None

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
            resolved_item_ids = [
                rid for rid in (_resolve_item(x) for x in (raw.get("source_item_ids") or []))
                if rid is not None
            ]
            resolved_src = _resolve_stance(raw.get("src_stance_id"))
            proposal = StanceProposal(
                kind=kind if kind in {"add", "rename"} else "add",  # type: ignore[arg-type]
                label=label,
                description=description,
                stance_type=stance_type,
                source_item_ids=resolved_item_ids,
                src_stance_id=resolved_src,
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
            src_id = _resolve_stance(raw.get("src_id"))
            dst_id = _resolve_stance(raw.get("dst_id"))
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
            sid = _resolve_stance(raw.get("stance_id"))
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
            from_id = _resolve_stance(raw.get("from_id"))
            to_id = _resolve_stance(raw.get("to_id"))
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

