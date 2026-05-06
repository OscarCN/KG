"""Phase 2 — tag a batch (stances + claims) in one LLM call.

The orchestrator does NOT mutate catalogs. It returns a `TaggingResult`
that the streaming runner threads through Phase 3 (adjudicator) and
Phase 4 (clusterer) before applying.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field
from typing import Optional

from src.entities.tags._llm_io import (
    call_cached,
    customer_context_block,
    load_prompt,
    render_prompt,
)
from src.entities.tags.models.claim_catalog import RawClaim
from src.entities.tags.models.customer import Customer
from src.entities.tags.models.source_item import SourceItem
from src.entities.tags.models.stance_catalog import StanceCatalog

logger = logging.getLogger(__name__)


_DEFAULT_MODEL = os.environ.get("OPENROUTER_TAGGER_MODEL", "openai/gpt-4o")
_PHASE = "tagging"


@dataclass
class StanceProposal:
    """A catalog mutation proposed by the tagging LLM. Adjudicated in
    Phase 3 before being applied (or rejected)."""

    kind: str  # "add" or "rename"
    label: str
    description: str
    src_stance_id: Optional[str] = None
    # Tag id used to route the assignments produced under this proposal —
    # the runner pre-creates a placeholder entry under this id so the
    # adjudicator's decision can re-point/drop those assignments.
    proposal_id: Optional[str] = None


@dataclass
class TaggingResult:
    stance_assignments: list[dict] = field(default_factory=list)
    stance_proposals: list[StanceProposal] = field(default_factory=list)
    claims: list[RawClaim] = field(default_factory=list)
    raw_claims_dropped_off_customer: int = 0


class TaggingOrchestrator:
    def __init__(
        self,
        customer: Customer,
        stance_catalog: StanceCatalog,
        model: str = _DEFAULT_MODEL,
    ):
        self.customer = customer
        self.stance_catalog = stance_catalog
        self.model = model

    # ──────────────────────────────────────────────────────────────

    def tag_batch(
        self,
        event_id: str,
        event_summary: str,
        items: list[SourceItem],
        use_cache: bool = True,
    ) -> TaggingResult:
        if not items:
            return TaggingResult()

        template = load_prompt("tagging")
        user_message = render_prompt(
            template,
            customer_context=customer_context_block(self.customer),
            filter_context=self.customer.filter_llm_prompt or "(sin filtro definido)",
            event_context=event_summary,
            stance_catalog=self._stance_catalog_block(),
            items_block=self._items_block(items),
        )
        messages = [{"role": "user", "content": user_message}]

        payload = {
            "phase": _PHASE,
            "customer_id": self.customer.entity_id,
            "event_id": event_id,
            "stance_catalog_snapshot": self._stance_catalog_snapshot(),
            "items": [
                {"id": it.id, "kind": it.kind, "text": it.text[:1200]}
                for it in items
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
            return TaggingResult()

        return self._build_result(event_id, items, parsed)

    # ──────────────────────────────────────────────────────────────

    def _build_result(
        self,
        event_id: str,
        items: list[SourceItem],
        parsed: dict,
    ) -> TaggingResult:
        kind_by_id = {it.id: it.kind for it in items}
        valid_stance_ids = set(self.stance_catalog.entries.keys())
        result = TaggingResult()

        for sa in parsed.get("stance_assignments") or []:
            sid = sa.get("source_item_id")
            stance_id = sa.get("stance_id")
            if not sid or stance_id not in valid_stance_ids:
                continue
            result.stance_assignments.append(
                {
                    "source_item_id": sid,
                    "source_kind": kind_by_id.get(sid, "article"),
                    "stance_id": stance_id,
                    "reason": (sa.get("reason") or "")[:500],
                }
            )

        for i, p in enumerate(parsed.get("stance_proposals") or []):
            kind = p.get("kind")
            label = (p.get("label") or "").strip()
            description = (p.get("description") or "").strip()
            if kind not in ("add", "rename") or not label:
                continue
            result.stance_proposals.append(
                StanceProposal(
                    kind=kind,
                    label=label,
                    description=description,
                    src_stance_id=p.get("src_stance_id"),
                    proposal_id=f"proposal_{i}",
                )
            )

        for c in parsed.get("claims") or []:
            sid = c.get("source_item_id")
            verbatim = (c.get("verbatim") or "").strip()
            affected = [int(x) for x in (c.get("affected_entity_ids") or []) if isinstance(x, (int, str)) and str(x).lstrip("-").isdigit()]
            if not sid or not verbatim:
                continue
            if self.customer.entity_id not in affected:
                result.raw_claims_dropped_off_customer += 1
                continue
            try:
                importance = int(c.get("importance") or 1)
            except (TypeError, ValueError):
                importance = 1
            importance = max(1, min(3, importance))
            result.claims.append(
                RawClaim(
                    event_id=event_id,
                    customer_id=self.customer.entity_id,
                    affected_entity_ids=affected,
                    verbatim=verbatim,
                    source_id=sid,
                    source_kind=kind_by_id.get(sid, "article"),
                    importance=importance,
                    importance_reason=(c.get("importance_reason") or "")[:500],
                )
            )

        return result

    def _stance_catalog_block(self) -> str:
        if not self.stance_catalog.entries:
            return "(catálogo vacío — toda postura nueva debe proponerse en stance_proposals)"
        lines = []
        for e in self.stance_catalog.entries.values():
            lines.append(f"- {e.id}: {e.label} — {e.description}")
        return "\n".join(lines)

    def _stance_catalog_snapshot(self) -> list[dict]:
        return [
            {"id": e.id, "label": e.label}
            for e in self.stance_catalog.entries.values()
        ]

    @staticmethod
    def _items_block(items: list[SourceItem]) -> str:
        lines = []
        for it in items:
            lines.append(f"- id={it.id} kind={it.kind}\n  {it.text[:1200]}")
        return "\n\n".join(lines)
