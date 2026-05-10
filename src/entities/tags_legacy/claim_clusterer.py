"""Phase 4 — cluster raw claims into the per-event claim catalog."""

from __future__ import annotations

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
from src.entities.tags.models.claim_catalog import ClaimCatalog, RawClaim
from src.entities.tags.models.customer import Customer

logger = logging.getLogger(__name__)


_DEFAULT_MODEL = os.environ.get(
    "OPENROUTER_CLUSTERER_MODEL", "google/gemini-2.5-flash-lite"
)
_PHASE = "clusterer"


@dataclass
class ClusteringDecision:
    claim_index: int
    decision: str  # "assign" / "create" / "drop"
    cluster_id: Optional[str] = None
    canonical: Optional[str] = None
    reason: str = ""


@dataclass
class ClusteringMutation:
    kind: str  # "rename" / "merge"
    cluster_id: Optional[str] = None
    new_canonical: Optional[str] = None
    src_id: Optional[str] = None
    dst_id: Optional[str] = None


@dataclass
class ClusteringResult:
    decisions: list[ClusteringDecision] = field(default_factory=list)
    mutations: list[ClusteringMutation] = field(default_factory=list)


class ClaimClusterer:
    def __init__(
        self,
        customer: Customer,
        claim_catalog: ClaimCatalog,
        event_summary: str,
        model: str = _DEFAULT_MODEL,
    ):
        self.customer = customer
        self.claim_catalog = claim_catalog
        self.event_summary = event_summary
        self.model = model

    def cluster(
        self,
        raw_claims: list[RawClaim],
        use_cache: bool = True,
    ) -> ClusteringResult:
        if not raw_claims:
            return ClusteringResult()

        template = load_prompt("clusterer")
        user_message = render_prompt(
            template,
            customer_context=customer_context_block(self.customer),
            event_context=self.event_summary,
            cluster_catalog=self._catalog_block(),
            claims_block=self._claims_block(raw_claims),
        )
        messages = [{"role": "user", "content": user_message}]

        payload = {
            "phase": _PHASE,
            "customer_id": self.customer.entity_id,
            "event_id": self.claim_catalog.event_id,
            "catalog_snapshot": [
                {"id": c.id, "canonical": c.canonical, "n_members": len(c.members)}
                for c in self.claim_catalog.clusters.values()
            ],
            "claims": [{"verbatim": rc.verbatim, "importance": rc.importance} for rc in raw_claims],
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
            return ClusteringResult()

        result = ClusteringResult()
        valid_cluster_ids = set(self.claim_catalog.clusters.keys())
        for d in parsed.get("decisions") or []:
            try:
                idx = int(d.get("claim_index"))
            except (TypeError, ValueError):
                continue
            if not (0 <= idx < len(raw_claims)):
                continue
            decision = d.get("decision")
            if decision not in ("assign", "create", "drop"):
                continue
            if decision == "assign":
                cid = d.get("cluster_id")
                if cid not in valid_cluster_ids:
                    logger.warning(
                        "clusterer returned assign with unknown cluster_id=%r — dropping",
                        cid,
                    )
                    continue
                result.decisions.append(
                    ClusteringDecision(claim_index=idx, decision="assign", cluster_id=cid)
                )
            elif decision == "create":
                canonical = (d.get("canonical") or "").strip()
                if not canonical:
                    continue
                result.decisions.append(
                    ClusteringDecision(
                        claim_index=idx, decision="create", canonical=canonical
                    )
                )
            else:  # drop
                result.decisions.append(
                    ClusteringDecision(
                        claim_index=idx,
                        decision="drop",
                        reason=(d.get("reason") or "")[:500],
                    )
                )

        for m in parsed.get("mutations") or []:
            kind = m.get("kind")
            if kind == "rename":
                cid = m.get("cluster_id")
                new_canonical = (m.get("new_canonical") or "").strip()
                if cid in valid_cluster_ids and new_canonical:
                    result.mutations.append(
                        ClusteringMutation(
                            kind="rename", cluster_id=cid, new_canonical=new_canonical
                        )
                    )
            elif kind == "merge":
                src = m.get("src_id")
                dst = m.get("dst_id")
                if src in valid_cluster_ids and dst in valid_cluster_ids and src != dst:
                    result.mutations.append(
                        ClusteringMutation(kind="merge", src_id=src, dst_id=dst)
                    )

        return result

    # ──────────────────────────────────────────────────────────────

    def _catalog_block(self) -> str:
        if not self.claim_catalog.clusters:
            return "(catálogo vacío — toda nueva afirmación va a 'create' o 'drop')"
        lines = []
        for c in self.claim_catalog.clusters.values():
            sample = [m.verbatim for m in c.members[:3]]
            lines.append(
                f"- {c.id} (n={len(c.members)}): {c.canonical}\n"
                f"    samples: {sample}"
            )
        return "\n".join(lines)

    @staticmethod
    def _claims_block(claims: list[RawClaim]) -> str:
        lines = []
        for i, rc in enumerate(claims):
            lines.append(f"[{i}] verbatim={rc.verbatim!r}  importance={rc.importance}")
        return "\n".join(lines)
