"""Bootstrap the per-customer typed stance catalog (design §5.1).

Three steps:
    1. Triage every item in the corpus via `TypeTriageStep`.
    2. Drop tag-only types (`noise`).
    3. Group `TypeTriageItem`s by stance_type; for each catalog-bearing
       type, run ONE `bootstrap_prompt_for_type` LLM call (single-shot,
       full occurrence set passed in) → list of validated `StanceEntry`s.
"""

from __future__ import annotations

import logging
from typing import Iterable, Optional

from src.entities.tags.catalogs import StanceCatalog
from src.entities.tags.llm import JsonLlm
from src.entities.tags.models import (
    ArticleBundle,
    Customer,
    SourceItem,
    StanceAssignment,
    StanceType,
    TypeTriageItem,
    now_iso,
)
from src.entities.tags.prompts import bootstrap_prompt_for_type
from src.entities.tags.streaming import STANCE_BEARING_ACTIVE_TYPES
from src.entities.tags.triage import TypeTriageStep


logger = logging.getLogger(__name__)


DEFAULT_MIN_EVIDENCE = 2
DEFAULT_MAX_PER_TYPE = 30


class BootstrapStep:
    """Build an initial typed catalog for one customer from a corpus."""

    def __init__(
        self,
        customer: Customer,
        triage_step: TypeTriageStep,
        llm: JsonLlm,
        *,
        min_evidence: int = DEFAULT_MIN_EVIDENCE,
        max_per_type: int = DEFAULT_MAX_PER_TYPE,
    ):
        self.customer = customer
        self.triage_step = triage_step
        self.llm = llm
        self.min_evidence = min_evidence
        self.max_per_type = max_per_type

    def run(
        self,
        corpus: Iterable[ArticleBundle],
        *,
        catalog: Optional[StanceCatalog] = None,
        query_id: Optional[int] = None,
    ) -> StanceCatalog:
        """Seed `catalog` from `corpus`. If `catalog` is None, a fresh
        in-memory `StanceCatalog` is created (the local-driver default);
        pass a `StanceCatalogRepo` to write bootstrap rows straight into
        userdb. The method surface used here (`add`, `assign`) is the
        same on both backends.

        `query_id` is stamped onto every bootstrap-time
        `stance_assignments` row via the catalog's items context
        (no-op for the in-memory backend; DB repo fills it on write).
        """
        if catalog is None:
            catalog = StanceCatalog(customer_id=self.customer.entity_id)

        # 1. Triage every item across the corpus.
        items_by_id: dict[str, SourceItem] = {}
        all_triaged: list[TypeTriageItem] = []
        for bundle in corpus:
            for it in bundle.all_items:
                items_by_id[it.id] = it
            triage = self.triage_step.triage(
                bundle.all_items,
                event=(bundle.linked_events[0] if bundle.linked_events else None),
            )
            all_triaged.extend(triage.triaged)
        logger.info(
            "bootstrap: triaged %d items across the corpus → %d rows",
            len(items_by_id),
            len(all_triaged),
        )

        # Wire enrichment context once for the whole bootstrap. The
        # DB-backed repo uses this to fill `parent_source_id` /
        # `news_type` / `query_id` on every assignment written below.
        catalog.set_items_context(items_by_id, query_id=query_id)

        # 2. Drop tag-only types and group by stance_type.
        per_type: dict[StanceType, list[TypeTriageItem]] = {}
        for row in all_triaged:
            if row.stance_type == "noise":
                continue
            per_type.setdefault(row.stance_type, []).append(row)

        # 3. Per type, one LLM call.
        for stance_type in STANCE_BEARING_ACTIVE_TYPES:
            occurrences = per_type.get(stance_type) or []

            # Pick one canonical hint per source_item_id — used both for
            # seeding catalogued assignments and for synthesizing null
            # assignments for IDs the LLM didn't cluster.
            first_hint_by_id: dict[str, TypeTriageItem] = {}
            for hint in occurrences:
                first_hint_by_id.setdefault(hint.source_item_id, hint)

            if len(occurrences) < self.min_evidence:
                # Too few hints to bootstrap, but every triaged item still
                # gets a null-stance row so the streaming-skip is safe.
                n_null = self._synth_null_assignments(
                    catalog, stance_type, first_hint_by_id, used_source_ids=set()
                )
                logger.info(
                    "bootstrap[%s]: skipping LLM (only %d occurrences) — %d null assignments synthesized",
                    stance_type, len(occurrences), n_null,
                )
                continue

            entries = self._bootstrap_one_type(
                stance_type, occurrences, items_by_id
            )

            used_source_ids: set[str] = set()
            for label, description, source_item_ids in entries:
                entry = catalog.add(
                    label=label,
                    description=description,
                    primary_type=stance_type,
                )
                # Seed catalogued assignments — first-write wins if the
                # LLM (incorrectly) put the same source_item_id in two clusters.
                for sid in source_item_ids:
                    if sid in used_source_ids:
                        continue
                    hint = first_hint_by_id.get(sid)
                    if hint is None:
                        continue
                    catalog.assign(StanceAssignment(
                        source_item_id=sid,
                        source_kind=hint.source_kind,
                        customer_id=self.customer.entity_id,
                        stance_id=entry.id,
                        stance_type=stance_type,
                        event_id=None,
                        reason=hint.brief_summary,
                        assigned_at=now_iso(),
                    ))
                    used_source_ids.add(sid)

            # Every remaining ID for this type → null assignment.
            n_null = self._synth_null_assignments(
                catalog, stance_type, first_hint_by_id, used_source_ids
            )
            logger.info(
                "bootstrap[%s]: %d entries, %d catalogued assignments, %d null assignments (from %d occurrences)",
                stance_type, len(entries), len(used_source_ids), n_null, len(occurrences),
            )

        return catalog

    def _synth_null_assignments(
        self,
        catalog: StanceCatalog,
        stance_type: StanceType,
        first_hint_by_id: dict[str, TypeTriageItem],
        used_source_ids: set[str],
    ) -> int:
        """One null-stance assignment per source_item_id not already
        catalogued for `stance_type`. Mirrors `tagging.py` synth-null
        logic so bootstrap output is behaviorally equivalent to a
        streaming-tagged pass over the same items."""
        n = 0
        for sid, hint in first_hint_by_id.items():
            if sid in used_source_ids:
                continue
            catalog.assign(StanceAssignment(
                source_item_id=sid,
                source_kind=hint.source_kind,
                customer_id=self.customer.entity_id,
                stance_id=None,
                stance_type=stance_type,
                event_id=None,
                reason=hint.brief_summary,
                assigned_at=now_iso(),
            ))
            n += 1
        return n

    # ── helpers ────────────────────────────────────────────────────────

    def _bootstrap_one_type(
        self,
        stance_type: StanceType,
        occurrences: list[TypeTriageItem],
        items_by_id: dict[str, SourceItem],
    ) -> list[tuple[str, str, list[str]]]:
        """Single-shot LLM call. Returns `(label, description,
        source_item_ids)` per surviving entry. The `source_item_ids`
        are the canonical IDs the LLM grouped into this entry — the
        caller uses them to seed `StanceAssignment` rows.

        For token efficiency the LLM sees compact integer ids (1..N); each
        comment is emitted immediately after its parent post so semantically
        related items sit together. `importance_hint` is not included.
        """
        # Group hints by their parent post (the post itself is its own group
        # root). post_order preserves first-appearance order across the corpus.
        by_post: dict[str, list[TypeTriageItem]] = {}
        post_order: list[str] = []
        orphan: list[TypeTriageItem] = []
        for hint in occurrences:
            item = items_by_id.get(hint.source_item_id)
            if item is None:
                orphan.append(hint)
                continue
            if item.kind == "user_comment" and item.parent_source_id:
                parent = item.parent_source_id
            else:
                parent = item.id
            if parent not in by_post:
                by_post[parent] = []
                post_order.append(parent)
            by_post[parent].append(hint)

        # Inside each group: post/article hints first, then user_comment hints
        # (stable sort preserves original order within each kind).
        def _kind_rank(h: TypeTriageItem) -> int:
            it = items_by_id.get(h.source_item_id)
            return 1 if (it and it.kind == "user_comment") else 0

        ordered: list[TypeTriageItem] = []
        for parent in post_order:
            ordered.extend(sorted(by_post[parent], key=_kind_rank))
        ordered.extend(orphan)

        # Compact integer id map (1..N) for the prompt; parser will reverse it.
        # `text` already lives on each TypeTriageItem (populated by the triage
        # step), so we don't re-look up the SourceItem here.
        id_map: dict[int, str] = {}
        payload: list[dict] = []
        for i, hint in enumerate(ordered, start=1):
            id_map[i] = hint.source_item_id
            payload.append(
                {
                    "id": i,
                    "kind": hint.source_kind,
                    "brief_summary": hint.brief_summary,
                    "text": hint.text,
                }
            )

        prompt = bootstrap_prompt_for_type(self.customer, stance_type, payload)
        response = self.llm.call(prompt)
        if not isinstance(response, dict):
            logger.warning("bootstrap[%s]: malformed response", stance_type)
            return []

        valid_canonical = {h.source_item_id for h in occurrences}
        out: list[tuple[str, str, list[str]]] = []
        for raw in response.get("entries") or []:
            if not isinstance(raw, dict):
                continue
            label = str(raw.get("label") or "").strip()
            if not label:
                continue
            description = str(raw.get("description") or "")
            # Map integer ids back to canonical ids; tolerate the legacy
            # canonical-string format too (in case a cached response still has it).
            ev_canonical: list[str] = []
            for x in raw.get("source_item_ids") or []:
                try:
                    xi = int(x)
                except (TypeError, ValueError):
                    xs = str(x)
                    if xs in valid_canonical:
                        ev_canonical.append(xs)
                    continue
                canonical = id_map.get(xi)
                if canonical and canonical in valid_canonical:
                    ev_canonical.append(canonical)
            # Dedup while preserving order.
            seen: set[str] = set()
            unique_ev: list[str] = []
            for sid in ev_canonical:
                if sid not in seen:
                    seen.add(sid)
                    unique_ev.append(sid)
            if len(unique_ev) < self.min_evidence:
                logger.debug(
                    "bootstrap[%s]: drop entry %r (only %d distinct evidence ids)",
                    stance_type, label, len(unique_ev),
                )
                continue
            out.append((label, description, unique_ev))
            if len(out) >= self.max_per_type:
                break
        return out
