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


def stance_catalog_hygiene_block(
    snapshot: list[dict],
    *,
    stance_id_map: dict[str, str],
    counts_by_id: dict[str, int],
    samples_by_id: dict[str, list[dict]],
) -> str:
    """Catalog slice for the hygiene prompt: short `st_N` ids, `n`
    catalogued-assignments counter, and `samples` (item text snippet +
    reason) per entry. Mutates `stance_id_map` in place."""
    payload: list[dict] = []
    for i, entry in enumerate(snapshot, start=1):
        short = f"st_{i}"
        canonical = entry["id"]
        stance_id_map[short] = canonical
        payload.append(
            {
                "id": short,
                "label": entry.get("label", ""),
                "description": entry.get("description", ""),
                "n": counts_by_id.get(canonical, 0),
                "samples": samples_by_id.get(canonical, []),
            }
        )
    return json.dumps(payload, ensure_ascii=False, indent=2)


def stance_catalog_short_block(
    snapshot: list[dict],
    *,
    stance_id_map: dict[str, str],
    counts_by_id: Optional[dict[str, int]] = None,
) -> str:
    """Catalog slice for prompts: short ids `st_N`, no `primary_type`.

    Mutates `stance_id_map` in place (short → canonical stance_id) so the
    parser can map the LLM's responses back. If `counts_by_id` is provided
    (canonical stance_id → n_assignments), each row also gets an `n` field
    so the LLM can see growth pressure (high `n` → maybe split / low `n` →
    maybe retire). Used by the consistency prompt; `tag_per_type` omits it.
    """
    payload: list[dict] = []
    for i, entry in enumerate(snapshot, start=1):
        short = f"st_{i}"
        canonical = entry["id"]
        stance_id_map[short] = canonical
        row: dict = {
            "id": short,
            "label": entry.get("label", ""),
            "description": entry.get("description", ""),
        }
        if counts_by_id is not None:
            row["n"] = counts_by_id.get(canonical, 0)
        payload.append(row)
    return json.dumps(payload, ensure_ascii=False, indent=2)


def post_texts_block(items: list[SourceItem], *, text_limit: int = 1200) -> str:
    """Just the post/article texts for tag_per_type — no ids, no kinds.

    Filters out `user_comment` items; comments live in the triage_hints
    block where each carries a `brief_summary` of what it expresses.
    """
    texts = [
        item.short_text(text_limit)
        for item in items
        if item.kind != "user_comment"
    ]
    return json.dumps(texts, ensure_ascii=False, indent=2, default=json_default)


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


def claim_clusters_canonicals_block(clusters: list[ClaimCluster]) -> str:
    """Cluster block for `claim_extract` — canonicals only.

    The extractor uses these purely as a de-dup signal and does NOT echo
    cluster ids back, so the `id` and `n_members` fields are pure waste.
    """
    return json.dumps(
        [c.canonical for c in clusters], ensure_ascii=False, indent=2
    )


def claim_clusters_short_block(
    clusters: list[ClaimCluster],
    *,
    cluster_id_map: dict[str, str],
) -> str:
    """Cluster block for `claim_group` — short `cl_N` ids, no `n_members`.

    Mutates `cluster_id_map` in place (short → canonical) so the parser
    can map the LLM's `cluster_id`/`src_id`/`dst_id` back.
    """
    payload: list[dict] = []
    for i, c in enumerate(clusters, start=1):
        short = f"cl_{i}"
        cluster_id_map[short] = c.id
        payload.append({"id": short, "canonical": c.canonical})
    return json.dumps(payload, ensure_ascii=False, indent=2)


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


def triage_hints_short_block(
    hints: list[TypeTriageItem],
    *,
    id_map: dict[int, str],
) -> str:
    """Triage hints for tag_per_type: int ids 1..N, no `stance_type`.

    Mutates `id_map` in place (int → canonical `source_item_id`) so the
    parser can map the LLM's responses back. The catalog is already
    type-filtered, so the stance_type field is redundant for the LLM.
    """
    payload: list[dict] = []
    for i, h in enumerate(hints, start=1):
        id_map[i] = h.source_item_id
        payload.append(
            {
                "id": i,
                "source_kind": h.source_kind,
                "brief_summary": h.brief_summary,
                "importance_hint": h.importance_hint,
                "text": h.text,
            }
        )
    return json.dumps(payload, ensure_ascii=False, indent=2)


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
    triage_id_map: Optional[dict[int, str]] = None,
    stance_id_map: Optional[dict[str, str]] = None,
) -> str:
    """Token-compact tag prompt.

    - `triage_id_map` (int → canonical `source_item_id`): postura ids.
    - `stance_id_map` (`st_N` → canonical stance_id): catalog short aliases.
    Both are mutated in place so the parser can map LLM responses back.
    Items appear above triage hints as raw post/article text (no ids).
    """
    tmap = triage_id_map if triage_id_map is not None else {}
    smap = stance_id_map if stance_id_map is not None else {}
    return render_prompt(
        load_prompt("tag_per_type"),
        customer=customer_block(customer),
        event=event_block(event),
        stance_type=stance_type,
        stance_type_guide=stance_type_guide(stance_type),
        catalog_slice=stance_catalog_short_block(catalog_slice, stance_id_map=smap),
        triage_hints=triage_hints_short_block(triage_hints, id_map=tmap),
        items=post_texts_block(items),
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
        existing_clusters=claim_clusters_canonicals_block(existing_clusters),
        items=items_block(items, id_map=id_map),
    )


def claim_group_prompt(
    customer: Customer,
    event: LinkedEventContext,
    raw_claims: list[RawClaim],
    existing_clusters: list[ClaimCluster],
    *,
    id_map: Optional[dict[int, int]] = None,
    cluster_id_map: Optional[dict[str, str]] = None,
) -> str:
    """`cluster_id_map` (`cl_N` → canonical cluster id) is mutated in place
    so the parser can map the LLM's `cluster_id`/`src_id`/`dst_id` back.
    """
    cmap = cluster_id_map if cluster_id_map is not None else {}
    return render_prompt(
        load_prompt("claim_group"),
        customer=customer_block_with_id(customer),
        event=event_block(event),
        existing_clusters=claim_clusters_short_block(existing_clusters, cluster_id_map=cmap),
        claims=raw_claims_block(raw_claims, id_map=id_map),
    )


def hygiene_prompt_for_type(
    customer: Customer,
    stance_type: StanceType,
    catalog_slice: list[dict],
    *,
    stance_id_map: Optional[dict[str, str]] = None,
    counts_by_id: Optional[dict[str, int]] = None,
    samples_by_id: Optional[dict[str, list[dict]]] = None,
) -> str:
    """Hygiene-pass prompt: merge_pairs + rename only, no items array.

    `counts_by_id` (canonical stance_id → n assignments) and
    `samples_by_id` (canonical stance_id → [{text, reason}]) are embedded
    per-entry so the LLM can judge similarity from real usage.
    """
    smap = stance_id_map if stance_id_map is not None else {}
    return render_prompt(
        load_prompt("hygiene_per_type"),
        customer=customer_block(customer),
        stance_type=stance_type,
        stance_type_guide=stance_type_guide(stance_type),
        catalog_slice=stance_catalog_hygiene_block(
            catalog_slice,
            stance_id_map=smap,
            counts_by_id=counts_by_id or {},
            samples_by_id=samples_by_id or {},
        ),
    )
