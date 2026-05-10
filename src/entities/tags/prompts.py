"""Prompt builders for the tags subsystem.

One builder per pipeline. Each composes the customer/event/items JSON blocks
into a `{name}` field map and renders the corresponding template under
`tags/prompts/`. Templates use the `{name}` placeholder convention (no
escaping required for JSON examples — only registered fields are
substituted).

Prompt content itself is rewritten in a follow-up slice (questions H1/H2);
this module is deliberately decoupled so the rewrite is mechanical.
"""

from __future__ import annotations

import json
from typing import Any, Optional

from src.entities.tags.llm import load_prompt, render_prompt
from src.entities.tags.models import (
    ClaimCluster,
    Customer,
    LinkedEventContext,
    RawClaim,
    SourceItem,
    StanceType,
    TypeTriageItem,
    json_default,
)


# ── Block builders ──────────────────────────────────────────────────────


def customer_block(customer: Customer) -> str:
    """Compact customer block — only the fields a prompt actually needs."""
    return json.dumps(
        {"name": customer.name, "description": customer.description},
        ensure_ascii=False,
        indent=2,
    )


def customer_block_with_id(customer: Customer) -> str:
    """Claim extraction needs the entity_id (to anchor the implicit-customer rule)."""
    return json.dumps(
        {
            "entity_id": customer.entity_id,
            "name": customer.name,
            "description": customer.description,
        },
        ensure_ascii=False,
        indent=2,
    )


def event_block(event: Optional[LinkedEventContext]) -> str:
    if event is None:
        return "null"
    return json.dumps({"description": event.description}, ensure_ascii=False, indent=2)


def items_block(
    items: list[SourceItem],
    *,
    text_limit: int = 1200,
    id_map: Optional[dict[int, str]] = None,
) -> str:
    """Compact item block keyed by local integer id (1-based).

    Mutates `id_map` in place if provided so the parser can map results
    back to canonical `SourceItem.id`.
    """
    payload = []
    for index, item in enumerate(items, start=1):
        if id_map is not None:
            id_map[index] = item.id
        payload.append(
            {
                "id": index,
                "kind": item.kind,
                "text": item.short_text(text_limit),
            }
        )
    return json.dumps(payload, ensure_ascii=False, indent=2, default=json_default)


def stance_catalog_block(snapshot: list[dict]) -> str:
    return json.dumps(snapshot, ensure_ascii=False, indent=2)


def claim_clusters_block(clusters: list[ClaimCluster]) -> str:
    return json.dumps(
        [
            {
                "id": c.id,
                "canonical": c.canonical,
                "n_members": len(c.members),
            }
            for c in clusters
        ],
        ensure_ascii=False,
        indent=2,
    )


def raw_claims_block(claims: list[RawClaim], *, id_map: Optional[dict[int, int]] = None) -> str:
    """Indexed claim list for `claim_group` prompts."""
    payload = []
    for index, claim in enumerate(claims):
        if id_map is not None:
            id_map[index] = index  # 0-based claim_index
        payload.append(
            {
                "claim_index": index,
                "verbatim": claim.verbatim,
                "importance": claim.importance,
                "importance_reason": claim.importance_reason,
                "source_item_id": claim.source_item_id,
            }
        )
    return json.dumps(payload, ensure_ascii=False, indent=2)


def triage_hints_block(hints: list[TypeTriageItem]) -> str:
    return json.dumps([h.to_dict() for h in hints], ensure_ascii=False, indent=2)


def stance_type_guide(stance_type: Optional[StanceType] = None) -> str:
    """Inject the per-type guide. If `stance_type` is None, concat all guides
    (used by triage and consistency-pass prompts).
    """
    if stance_type is not None:
        return load_prompt(f"types/{stance_type}")
    parts = []
    for t in (
        "entity_stance",
        "complaint",
        "gratefulness",
        "suggestion",
        "request",
        "denuncia",
        "question",
        "endorsement",
        "noise",
    ):
        parts.append(load_prompt(f"types/{t}"))
    return "\n\n".join(parts)


# ── Pipeline-level builders ─────────────────────────────────────────────


def triage_prompt(
    customer: Customer,
    items: list[SourceItem],
    event: Optional[LinkedEventContext] = None,
    *,
    id_map: Optional[dict[int, str]] = None,
) -> str:
    return render_prompt(
        load_prompt("triage"),
        customer=customer_block(customer),
        event=event_block(event),
        items=items_block(items, id_map=id_map),
        stance_type_guide=stance_type_guide(None),
    )


def bootstrap_prompt_for_type(
    customer: Customer,
    stance_type: StanceType,
    occurrences: list[dict[str, Any]],
) -> str:
    """`occurrences` is a flat list of `{source_item_id, text, brief_summary}`
    rows for items the triage flagged as carrying this stance type.
    """
    return render_prompt(
        load_prompt("bootstrap_per_type"),
        customer=customer_block(customer),
        stance_type=stance_type,
        stance_type_guide=stance_type_guide(stance_type),
        occurrences=json.dumps(occurrences, ensure_ascii=False, indent=2, default=json_default),
    )


def tag_prompt_for_type(
    customer: Customer,
    items: list[SourceItem],
    triage_hints: list[TypeTriageItem],
    catalog_slice: list[dict],
    stance_type: StanceType,
    *,
    event: Optional[LinkedEventContext] = None,
    id_map: Optional[dict[int, str]] = None,
) -> str:
    return render_prompt(
        load_prompt("tag_per_type"),
        customer=customer_block(customer),
        event=event_block(event),
        stance_type=stance_type,
        stance_type_guide=stance_type_guide(stance_type),
        catalog_slice=stance_catalog_block(catalog_slice),
        triage_hints=triage_hints_block(triage_hints),
        items=items_block(items, id_map=id_map),
    )


def claim_extract_prompt(
    customer: Customer,
    event: LinkedEventContext,
    items: list[SourceItem],
    existing_clusters: list[ClaimCluster],
    *,
    id_map: Optional[dict[int, str]] = None,
) -> str:
    # Note: comment filtering happens BEFORE this call (in
    # `ClaimTagger.tag` — it builds `items` from `[root]` plus optionally
    # the comments). The prompt no longer carries an `include_comments`
    # flag — the model just processes whatever items it receives.
    return render_prompt(
        load_prompt("claim_extract"),
        customer=customer_block_with_id(customer),
        event=event_block(event),
        existing_clusters=claim_clusters_block(existing_clusters),
        items=items_block(items, id_map=id_map),
    )


def claim_group_prompt(
    customer: Customer,
    event: LinkedEventContext,
    raw_claims: list[RawClaim],
    existing_clusters: list[ClaimCluster],
    *,
    id_map: Optional[dict[int, int]] = None,
) -> str:
    return render_prompt(
        load_prompt("claim_group"),
        customer=customer_block_with_id(customer),
        event=event_block(event),
        existing_clusters=claim_clusters_block(existing_clusters),
        claims=raw_claims_block(raw_claims, id_map=id_map),
    )


def consistency_prompt_for_type(
    customer: Customer,
    stance_type: StanceType,
    catalog_slice: list[dict],
    assignment_sample: list[dict],
    item_samples: list[dict],
    claim_summaries: list[dict],
) -> str:
    return render_prompt(
        load_prompt("consistency_per_type"),
        customer=customer_block(customer),
        stance_type=stance_type,
        stance_type_guide=stance_type_guide(stance_type),
        catalog_slice=stance_catalog_block(catalog_slice),
        assignments=json.dumps(
            assignment_sample, ensure_ascii=False, indent=2, default=json_default
        ),
        items=json.dumps(item_samples, ensure_ascii=False, indent=2, default=json_default),
        claim_summaries=json.dumps(
            claim_summaries, ensure_ascii=False, indent=2, default=json_default
        ),
    )
