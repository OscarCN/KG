"""Periodic consistency pass — three stages per active stance type.

Stage 1 — Deterministic retire (no LLM): retire entries with zero
    all-time catalogued assignments.
Stage 2 — Orphan bootstrap (one LLM call per type): cluster null-stance
    assignments in the recent-bundle window into new entries, reusing
    BootstrapStep._bootstrap_one_type.
Stage 3 — Hygiene (one LLM call per type): merge near-duplicate entries
    and rename entries with poor labels. Input: catalog entries with per-
    entry `n` and a small sample of {text, reason} pairs. No full items
    array, no full assignments array, no add/retire/reroute.
"""

from __future__ import annotations

import logging
import math
from typing import Optional

from src.entities.tags.bootstrap import BootstrapStep
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
    TypeTriageItem,
    now_iso,
)
from src.entities.tags.prompts import hygiene_prompt_for_type
from src.entities.tags.streaming import STANCE_BEARING_ACTIVE_TYPES


logger = logging.getLogger(__name__)


DEFAULT_ITEM_SAMPLES_PER_CALL = 30
DEFAULT_WINDOW_MULTIPLIER = 1.25
DEFAULT_HYGIENE_SAMPLES_PER_ENTRY = 5


class ConsistencyPassStep:
    def __init__(
        self,
        customer: Customer,
        llm: JsonLlm,
        *,
        bootstrap_step: Optional[BootstrapStep] = None,
        window_multiplier: float = DEFAULT_WINDOW_MULTIPLIER,
    ):
        self.customer = customer
        self.llm = llm
        # Stage 2 (orphan bootstrap) reuses BootstrapStep's clustering
        # logic; if None, Stage 2 is skipped.
        self.bootstrap_step = bootstrap_step
        self.window_multiplier = window_multiplier

    def run(
        self,
        catalog: StanceCatalog,
        items_seen: dict[str, SourceItem],
    ) -> ConsistencyPassResult:
        result = ConsistencyPassResult(customer_id=self.customer.entity_id)
        result.started_at = now_iso()
        summary = StepSummary(name="consistency_pass")

        # Stage 1 — deterministic retire (no LLM). Entries with zero
        # catalogued assignments are dropped before any LLM stage runs.
        # Cheap, eliminates obvious dead entries from later prompts.
        self._stage1_deterministic_retire(catalog, result, summary)

        # Window: most recent K bundles (= unique post/article
        # source_item_ids). Sized by 1.25× the bundles processed since
        # the last consistency pass. Comments are excluded for now —
        # they're folded in later.
        bundles_since = max(0, self.customer.bundles_processed_since_last_pass)
        n_bundles = math.ceil(bundles_since * self.window_multiplier)
        window = catalog.recent_bundle_assignments(
            n_bundles=n_bundles,
            kinds=("article", "user_post"),
        )
        window_per_type: dict[StanceType, list[StanceAssignment]] = {}
        for a in window:
            window_per_type.setdefault(a.stance_type, []).append(a)
        logger.info(
            "consistency window: %d bundles, %d assignments (since last pass: %d bundles)",
            n_bundles, len(window), bundles_since,
        )

        # Stage 2 — orphan bootstrap. Cluster null-stance assignments in
        # the window into new entries; route the nulls to those entries.
        if self.bootstrap_step is not None:
            self._stage2_orphan_bootstrap(
                catalog, items_seen, window_per_type, result, summary,
            )

        # Stage 3 — hygiene (merge + rename only). One LLM call per type.
        # Inputs: catalog entries with per-entry `n` and a small sample of
        # {text, reason} pairs drawn from the window. No items array, no
        # full assignments array, no add/retire/reroute.
        for stance_type in STANCE_BEARING_ACTIVE_TYPES:
            type_window = window_per_type.get(stance_type) or []
            self._stage3_hygiene(
                catalog, items_seen, stance_type, type_window, result, summary,
            )

        # Update customer counters.
        self.customer.last_consistency_pass_at = now_iso()
        self.customer.items_processed_since_last_pass = 0
        self.customer.bundles_processed_since_last_pass = 0

        result.finished_at = now_iso()
        result.summary = summary
        return result

    # ── stages ─────────────────────────────────────────────────────────

    def _stage1_deterministic_retire(
        self,
        catalog: StanceCatalog,
        result: ConsistencyPassResult,
        summary: StepSummary,
    ) -> None:
        """Retire entries with zero catalogued assignments. No LLM.

        Counts are computed over the FULL assignment list (all-time),
        not just the consistency window — an entry that's never been
        used is dead regardless of recency window. Records each retire
        in `result.retire_ids` and bumps a per-type summary counter.
        """
        counts: dict[str, int] = {}
        for a in catalog.assignments:
            if a.stance_id:
                counts[a.stance_id] = counts.get(a.stance_id, 0) + 1
        # Snapshot before iterating since retire() mutates `entries`.
        candidates = [
            (eid, entry.primary_type)
            for eid, entry in catalog.entries.items()
            if counts.get(eid, 0) == 0
        ]
        for eid, primary_type in candidates:
            if catalog.retire(eid):
                result.retire_ids.append(eid)
                summary.inc(f"{primary_type}_stage1_retire_applied")
        if candidates:
            logger.info(
                "consistency stage1: retired %d entry(ies) with zero catalogued assignments",
                len(candidates),
            )

    def _stage2_orphan_bootstrap(
        self,
        catalog: StanceCatalog,
        items_seen: dict[str, SourceItem],
        window_per_type: dict[StanceType, list[StanceAssignment]],
        result: ConsistencyPassResult,
        summary: StepSummary,
    ) -> None:
        """Cluster null-stance assignments in the window into new
        entries via the same code path as Phase-1 bootstrap.

        For each stance type:
        1. Collect null-stance assignments in the window.
        2. If below `min_evidence`, skip (no LLM call).
        3. Rebuild `TypeTriageItem`s from those assignments (the
           assignment carries everything we need except `text`, which
           comes from `items_seen`).
        4. Call `BootstrapStep._bootstrap_one_type` to cluster.
        5. For each new entry, add to catalog and re-route the
           matching null assignments to the new entry (in place,
           preserving `assigned_at`).
        """
        assert self.bootstrap_step is not None  # gated by caller
        for stance_type in STANCE_BEARING_ACTIVE_TYPES:
            assignments_for_type = window_per_type.get(stance_type) or []
            null_rows = [a for a in assignments_for_type if a.stance_id is None]
            if len(null_rows) < self.bootstrap_step.min_evidence:
                if null_rows:
                    summary.inc(
                        f"{stance_type}_stage2_skipped_small_orphan_pool",
                        len(null_rows),
                    )
                continue
            # Reconstruct triage-item shape for the existing bootstrap helper.
            triage_hints: list[TypeTriageItem] = []
            for a in null_rows:
                item = items_seen.get(a.source_item_id)
                text = item.short_text(800) if item else ""
                triage_hints.append(TypeTriageItem(
                    source_item_id=a.source_item_id,
                    source_kind=a.source_kind,
                    stance_type=stance_type,
                    brief_summary=a.reason,
                    importance_hint=None,
                    text=text,
                ))
            entries = self.bootstrap_step._bootstrap_one_type(
                stance_type, triage_hints, items_seen,
            )
            for label, description, source_item_ids in entries:
                # De-dup against an existing entry of the same type with
                # the same normalized label (Stage 3 will catch the rest).
                norm = label.strip().lower()
                existing = next(
                    (
                        e for e in catalog.iter_entries(types=[stance_type])
                        if e.label.strip().lower() == norm
                    ),
                    None,
                )
                if existing is not None:
                    summary.inc(f"{stance_type}_stage2_add_already_exists")
                    target_entry_id = existing.id
                else:
                    entry = catalog.add(
                        label=label,
                        description=description,
                        primary_type=stance_type,
                    )
                    target_entry_id = entry.id
                    result.proposals.append(StanceProposal(
                        kind="add",
                        label=label,
                        description=description,
                        stance_type=stance_type,
                        source_item_ids=source_item_ids,
                    ))
                    summary.inc(f"{stance_type}_stage2_add_applied")
                # Route the matching null assignments to this entry.
                moved = self._route_nulls_to_entry(
                    catalog, stance_type, target_entry_id, source_item_ids,
                )
                summary.inc(f"{stance_type}_stage2_nulls_routed", moved)
            logger.info(
                "consistency stage2[%s]: %d orphans → %d new/matched entries",
                stance_type, len(null_rows), len(entries),
            )

    @staticmethod
    def _route_nulls_to_entry(
        catalog: StanceCatalog,
        stance_type: StanceType,
        entry_id: str,
        source_item_ids: list[str],
    ) -> int:
        """Mutate matching null-stance assignments in place. Equivalent
        to a targeted reroute(None → entry_id) restricted to the given
        source_item_ids and stance_type. Preserves `assigned_at`."""
        targets = set(source_item_ids)
        n = 0
        for a in catalog.assignments:
            if (
                a.stance_id is None
                and a.stance_type == stance_type
                and a.source_item_id in targets
            ):
                a.stance_id = entry_id
                n += 1
        return n

    # ── stage 3 ────────────────────────────────────────────────────────

    def _stage3_hygiene(
        self,
        catalog: StanceCatalog,
        items_seen: dict[str, SourceItem],
        stance_type: StanceType,
        type_window: list[StanceAssignment],
        result: ConsistencyPassResult,
        summary: StepSummary,
    ) -> None:
        """Hygiene pass: merge_pairs + rename only. One LLM call per type.

        Builds per-entry {text, reason} samples from the window assignments
        (capped at DEFAULT_HYGIENE_SAMPLES_PER_ENTRY), then calls the
        hygiene_per_type prompt.  No items array, no full assignments array.
        """
        catalog_slice = catalog.snapshot(types=[stance_type])
        if not catalog_slice:
            return
        existing_ids = {entry["id"] for entry in catalog_slice}

        # Per-entry assignment counts (all-time, not just window).
        counts_by_id: dict[str, int] = {}
        for a in catalog.assignments:
            if a.stance_type == stance_type and a.stance_id:
                counts_by_id[a.stance_id] = counts_by_id.get(a.stance_id, 0) + 1

        # Per-entry sample: up to N {text snippet, reason} pairs.
        samples_by_id: dict[str, list[dict]] = {}
        for a in type_window:
            if not a.stance_id:
                continue
            bucket = samples_by_id.setdefault(a.stance_id, [])
            if len(bucket) >= DEFAULT_HYGIENE_SAMPLES_PER_ENTRY:
                continue
            item = items_seen.get(a.source_item_id)
            text = item.short_text(300) if item else ""
            bucket.append({"text": text, "reason": a.reason or ""})

        stance_id_map: dict[str, str] = {}
        prompt = hygiene_prompt_for_type(
            self.customer,
            stance_type,
            catalog_slice,
            stance_id_map=stance_id_map,
            counts_by_id=counts_by_id,
            samples_by_id=samples_by_id,
        )
        response = self.llm.call(prompt)
        if not isinstance(response, dict):
            logger.warning("consistency stage3[%s]: malformed response", stance_type)
            return

        def _resolve(raw_id) -> Optional[str]:
            if raw_id in (None, "", "null") or not isinstance(raw_id, str):
                return None
            canonical = stance_id_map.get(raw_id)
            if canonical is not None:
                return canonical
            return raw_id if raw_id in existing_ids else None

        # merges (N-way: ids[-1] survives, absorbs all others)
        for raw in response.get("merges") or []:
            if not isinstance(raw, dict):
                continue
            raw_ids = raw.get("ids") or []
            if not isinstance(raw_ids, list) or len(raw_ids) < 2:
                summary.inc(f"{stance_type}_stage3_merge_dropped_invalid")
                continue
            resolved = [_resolve(rid) for rid in raw_ids]
            resolved = [r for r in resolved if r and r in existing_ids]
            if len(resolved) < 2:
                summary.inc(f"{stance_type}_stage3_merge_dropped_invalid")
                continue
            dst_id = resolved[-1]
            src_ids = resolved[:-1]
            cross_type = any(
                catalog.entries.get(s) and catalog.entries.get(dst_id)
                and catalog.entries[s].primary_type != catalog.entries[dst_id].primary_type
                for s in src_ids
            )
            if cross_type:
                summary.inc(f"{stance_type}_stage3_merge_dropped_cross_type")
                continue
            merged_any = False
            for src_id in src_ids:
                moved = catalog.merge(src_id, dst_id)
                if moved or src_id not in catalog.entries:
                    result.merge_pairs.append((src_id, dst_id))
                    existing_ids.discard(src_id)
                    merged_any = True
            if merged_any:
                new_label = str(raw.get("new_label") or "").strip()
                new_description = str(raw.get("new_description") or "").strip()
                if new_label and dst_id in catalog.entries:
                    catalog.rename(dst_id, new_label, new_description or catalog.entries[dst_id].description)
                summary.inc(f"{stance_type}_stage3_merge_applied")

        # rename
        for raw in response.get("rename") or []:
            if not isinstance(raw, dict):
                continue
            src_id = _resolve(raw.get("src_stance_id"))
            label = str(raw.get("label") or "").strip()
            description = str(raw.get("description") or "")
            if src_id and label and src_id in existing_ids:
                if catalog.rename(src_id, label, description):
                    result.proposals.append(StanceProposal(
                        kind="rename",
                        label=label,
                        description=description,
                        stance_type=stance_type,
                        src_stance_id=src_id,
                    ))
                    summary.inc(f"{stance_type}_stage3_rename_applied")
                else:
                    summary.inc(f"{stance_type}_stage3_rename_dropped")
            else:
                summary.inc(f"{stance_type}_stage3_rename_dropped_invalid")

        logger.info(
            "consistency stage3[%s]: merges=%d renames=%d",
            stance_type,
            sum(1 for p in result.merge_pairs),
            sum(1 for p in result.proposals if p.kind == "rename" and p.stance_type == stance_type),
        )

