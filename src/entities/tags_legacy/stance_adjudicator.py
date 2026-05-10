"""Phase 3 — adjudicate proposed stance catalog mutations."""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from typing import Optional

from src.entities.tags._llm_io import (
    call_cached,
    customer_context_block,
    load_prompt,
    render_prompt,
)
from src.entities.tags.models.customer import Customer
from src.entities.tags.models.source_item import SourceItem
from src.entities.tags.models.stance_catalog import StanceCatalog
from src.entities.tags.tagging import StanceProposal

logger = logging.getLogger(__name__)


_DEFAULT_MODEL = os.environ.get("OPENROUTER_ADJUDICATOR_MODEL", "openai/gpt-4o")
_PHASE = "adjudicator"


@dataclass
class AdjudicationDecision:
    proposal_index: int
    decision: str  # "accept" / "reject" / "rename" / "generalise"
    existing_id: Optional[str] = None
    new_label: Optional[str] = None
    new_description: Optional[str] = None
    reason: str = ""


class StanceAdjudicator:
    def __init__(
        self,
        customer: Customer,
        stance_catalog: StanceCatalog,
        model: str = _DEFAULT_MODEL,
    ):
        self.customer = customer
        self.stance_catalog = stance_catalog
        self.model = model

    def adjudicate(
        self,
        proposals: list[StanceProposal],
        sample_items: list[SourceItem],
        use_cache: bool = True,
    ) -> list[AdjudicationDecision]:
        if not proposals:
            return []

        template = load_prompt("adjudicator")
        user_message = render_prompt(
            template,
            customer_context=customer_context_block(self.customer),
            stance_catalog=self._catalog_block(),
            proposals_block=self._proposals_block(proposals),
            sample_items_block=self._sample_block(sample_items),
        )
        messages = [{"role": "user", "content": user_message}]

        payload = {
            "phase": _PHASE,
            "customer_id": self.customer.entity_id,
            "catalog_ids": sorted(self.stance_catalog.entries.keys()),
            "proposals": [
                {"kind": p.kind, "label": p.label, "src_stance_id": p.src_stance_id}
                for p in proposals
            ],
        }
        parsed = call_cached(
            phase=_PHASE,
            customer_id=self.customer.entity_id,
            payload=payload,
            messages=messages,
            model=self.model,
            use_cache=use_cache,
        )
        if not parsed:
            return []

        out: list[AdjudicationDecision] = []
        valid_ids = set(self.stance_catalog.entries.keys())
        for d in parsed.get("decisions") or []:
            try:
                idx = int(d.get("proposal_index"))
            except (TypeError, ValueError):
                continue
            if not (0 <= idx < len(proposals)):
                continue
            decision = d.get("decision")
            if decision not in ("accept", "reject", "rename", "generalise"):
                continue
            existing_id = d.get("existing_id")
            if decision in ("rename", "generalise") and existing_id not in valid_ids:
                logger.warning(
                    "adjudicator returned %s with unknown existing_id=%r — "
                    "treating as reject",
                    decision,
                    existing_id,
                )
                decision = "reject"
                existing_id = None
            out.append(
                AdjudicationDecision(
                    proposal_index=idx,
                    decision=decision,
                    existing_id=existing_id,
                    new_label=d.get("new_label"),
                    new_description=d.get("new_description"),
                    reason=(d.get("reason") or "")[:500],
                )
            )
        return out

    # ──────────────────────────────────────────────────────────────

    def _catalog_block(self) -> str:
        if not self.stance_catalog.entries:
            return "(catálogo vacío)"
        return "\n".join(
            f"- {e.id} (n={e.n_assignments}): {e.label} — {e.description}"
            for e in self.stance_catalog.entries.values()
        )

    @staticmethod
    def _proposals_block(proposals: list[StanceProposal]) -> str:
        lines = []
        for i, p in enumerate(proposals):
            line = f"[{i}] kind={p.kind} label={p.label!r}"
            if p.src_stance_id:
                line += f" src_stance_id={p.src_stance_id}"
            if p.description:
                line += f"\n    description: {p.description}"
            lines.append(line)
        return "\n".join(lines)

    @staticmethod
    def _sample_block(items: list[SourceItem]) -> str:
        if not items:
            return "(sin muestras)"
        lines = []
        for it in items[:8]:
            lines.append(f"- [{it.kind}] {it.id}: {it.text[:600]}")
        return "\n".join(lines)
