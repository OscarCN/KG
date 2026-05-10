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
    StanceType,
    TypeTriageItem,
)
from src.entities.tags.prompts import bootstrap_prompt_for_type
from src.entities.tags.streaming import STANCE_BEARING_ACTIVE_TYPES
from src.entities.tags.triage import TypeTriageStep


logger = logging.getLogger(__name__)


DEFAULT_MIN_EVIDENCE = 2
DEFAULT_MAX_PER_TYPE = 15


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

    def run(self, corpus: Iterable[ArticleBundle]) -> StanceCatalog:
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

        # 2. Drop tag-only types and group by stance_type.
        per_type: dict[StanceType, list[TypeTriageItem]] = {}
        for row in all_triaged:
            if row.stance_type == "noise":
                continue
            per_type.setdefault(row.stance_type, []).append(row)

        # 3. Per type, one LLM call.
        for stance_type in STANCE_BEARING_ACTIVE_TYPES:
            occurrences = per_type.get(stance_type) or []
            if len(occurrences) < self.min_evidence:
                logger.info(
                    "bootstrap[%s]: skipping (only %d occurrences, need ≥%d)",
                    stance_type,
                    len(occurrences),
                    self.min_evidence,
                )
                continue
            entries = self._bootstrap_one_type(
                stance_type, occurrences, items_by_id
            )
            for label, description, evidence_count in entries:
                catalog.add(
                    label=label,
                    description=description,
                    primary_type=stance_type,
                )
            logger.info(
                "bootstrap[%s]: created %d entries from %d occurrences",
                stance_type,
                len(entries),
                len(occurrences),
            )

        return catalog

    # ── helpers ────────────────────────────────────────────────────────

    def _bootstrap_one_type(
        self,
        stance_type: StanceType,
        occurrences: list[TypeTriageItem],
        items_by_id: dict[str, SourceItem],
    ) -> list[tuple[str, str, int]]:
        """Single-shot LLM call. Returns list of (label, description, evidence_count)."""
        payload = []
        for hint in occurrences:
            item = items_by_id.get(hint.source_item_id)
            payload.append(
                {
                    "source_item_id": hint.source_item_id,
                    "kind": hint.source_kind,
                    "brief_summary": hint.brief_summary,
                    "text": item.short_text(800) if item else "",
                    "importance_hint": hint.importance_hint,
                }
            )
        prompt = bootstrap_prompt_for_type(self.customer, stance_type, payload)
        response = self.llm.call(prompt)
        if not isinstance(response, dict):
            logger.warning("bootstrap[%s]: malformed response", stance_type)
            return []

        valid_ids = {h.source_item_id for h in occurrences}
        out: list[tuple[str, str, int]] = []
        for raw in response.get("entries") or []:
            if not isinstance(raw, dict):
                continue
            label = str(raw.get("label") or "").strip()
            if not label:
                continue
            description = str(raw.get("description") or "")
            ev_ids = [
                str(x) for x in (raw.get("source_item_ids") or [])
                if str(x) in valid_ids
            ]
            if len(set(ev_ids)) < self.min_evidence:
                logger.debug(
                    "bootstrap[%s]: drop entry %r (only %d distinct evidence ids)",
                    stance_type, label, len(set(ev_ids)),
                )
                continue
            out.append((label, description, len(set(ev_ids))))
            if len(out) >= self.max_per_type:
                break
        return out
