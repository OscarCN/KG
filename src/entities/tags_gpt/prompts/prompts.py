"""Prompt builders for the decoupled tags steps."""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path
from string import Template
from typing import Any

from src.entities.tags_gpt.models import (
    Customer,
    LinkedEvent,
    SourceItem,
    StanceType,
    TypeTriageItem,
    json_default,
)


_TEXT_DIR = Path(__file__).with_name("text")


@lru_cache(maxsize=None)
def _text(name: str) -> str:
    return (_TEXT_DIR / name).read_text(encoding="utf-8").strip()


def _render(name: str, **values: Any) -> str:
    payload = {key: str(value) for key, value in values.items()}
    return Template(_text(name)).safe_substitute(payload).strip()


def customer_block(customer: Customer) -> str:
    return json.dumps(
        {
            "entity_id": customer.entity_id,
            "name": customer.name,
            "description": customer.description,
            "aliases": customer.aliases,
            "types": [x.to_dict() for x in customer.types],
            "filter_context": customer.filter_llm_prompt,
        },
        ensure_ascii=False,
        indent=2,
        default=json_default,
    )


def compact_customer_payload(
    customer: Customer,
    *,
    include_entity_id: bool = False,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "name": customer.name,
        "description": customer.description,
    }
    if include_entity_id:
        payload = {"entity_id": customer.entity_id, **payload}
    return payload


def compact_customer_block(
    customer: Customer,
    *,
    include_entity_id: bool = False,
) -> str:
    return json.dumps(
        compact_customer_payload(customer, include_entity_id=include_entity_id),
        ensure_ascii=False,
        indent=2,
        default=json_default,
    )


def event_block(event: LinkedEvent) -> str:
    return json.dumps(
        {
            "id": event.id,
            "event_type": event.event_type,
            "name": event.name,
            "description": event.description,
            "date_range": event.date_range,
            "location": event.location,
            "source_ids": event.source_ids,
        },
        ensure_ascii=False,
        indent=2,
        default=json_default,
    )


def compact_event_payload(event: LinkedEvent) -> dict[str, Any]:
    return {
        "description": event.description,
    }


def compact_event_block(event: LinkedEvent) -> str:
    return json.dumps(
        compact_event_payload(event),
        ensure_ascii=False,
        indent=2,
        default=json_default,
    )


def items_block(items: list[SourceItem], *, text_limit: int = 1200) -> str:
    payload = [
        {
            "id": item.id,
            "kind": item.kind,
            "text": item.short_text(text_limit),
            "author": item.author,
            "created_at": item.created_at,
            "parent_source_id": item.parent_source_id,
        }
        for item in items
    ]
    return json.dumps(payload, ensure_ascii=False, indent=2, default=json_default)


def compact_items_payload(
    items: list[dict[str, Any]] | list[SourceItem],
    *,
    text_limit: int = 1200,
) -> list[dict[str, Any]] | list[SourceItem]:
    if items and isinstance(items[0], SourceItem):
        return [
            {
                "id": index,
                "kind": item.kind,
                "text": item.short_text(text_limit),
            }
            for index, item in enumerate(items, start=1)
        ]
    return items


def compact_items_block(
    items: list[dict[str, Any]] | list[SourceItem],
    *,
    text_limit: int = 1200,
) -> str:
    return json.dumps(
        compact_items_payload(items, text_limit=text_limit),
        ensure_ascii=False,
        indent=2,
        default=json_default,
    )


def triage_customer_block(customer: Customer) -> str:
    return compact_customer_block(customer)


def triage_event_block(event: LinkedEvent) -> str:
    return compact_event_block(event)


def triage_items_block(items: list[dict[str, Any]] | list[SourceItem]) -> str:
    return compact_items_block(items)


def stance_catalog_block(entries: list[dict[str, Any]]) -> str:
    return json.dumps(entries, ensure_ascii=False, indent=2, default=json_default)


def claim_catalog_block(clusters: list[dict[str, Any]]) -> str:
    return json.dumps(clusters, ensure_ascii=False, indent=2, default=json_default)


def indexed_block(values: list[dict[str, Any]], index_name: str) -> str:
    payload = []
    for index, value in enumerate(values):
        item = dict(value)
        item[index_name] = index
        payload.append(item)
    return json.dumps(payload, ensure_ascii=False, indent=2, default=json_default)


STANCE_RUBRIC = _text("stance_rubric.txt")


STANCE_FORMAT_RULES = _text("stance_format_rules.txt")


STANCE_TYPE_TIE_BREAK = (
    "denuncia > request > complaint > suggestion > gratefulness > "
    "endorsement > entity_stance > question > noise"
)


STANCE_TYPE_GUIDES: dict[str, str] = {
    "entity_stance": _text("stance_types/entity_stance.txt"),
    "complaint": _text("stance_types/complaint.txt"),
    "gratefulness": _text("stance_types/gratefulness.txt"),
    "suggestion": _text("stance_types/suggestion.txt"),
    "request": _text("stance_types/request.txt"),
    "denuncia": _text("stance_types/denuncia.txt"),
    "question": _text("stance_types/question.txt"),
    "endorsement": _text("stance_types/endorsement.txt"),
    "noise": _text("stance_types/noise.txt"),
}


def stance_type_guide(stance_type: StanceType | None = None) -> str:
    if stance_type:
        return STANCE_TYPE_GUIDES[stance_type]
    return "\n\n".join(STANCE_TYPE_GUIDES.values())


def triage_stance_type_guide() -> str:
    sections = []
    for guide in STANCE_TYPE_GUIDES.values():
        lines = [
            line
            for line in guide.splitlines()
            if "sentiment" not in line.lower()
        ]
        sections.append("\n".join(lines).strip())
    return "\n\n".join(sections)


_CLAIM_RULES_NO_COMMENTS = _text("claim_rules_no_comments.txt")
_CLAIM_RULES_INCLUDE_COMMENTS = _text("claim_rules_include_comments.txt")


def _claim_extraction_rules(include_comments: bool) -> str:
    source_rule = (
        _CLAIM_RULES_INCLUDE_COMMENTS if include_comments else _CLAIM_RULES_NO_COMMENTS
    )
    return _render("claim_extraction_rules.txt", source_rule=source_rule)


# Default value (no comments) preserved for callers that imported the constant.
CLAIM_EXTRACTION_RULES = _claim_extraction_rules(include_comments=False)


CLAIM_CLUSTER_RULES = _text("claim_cluster_rules.txt")


def bootstrap_prompt(
    customer: Customer,
    items: list[dict[str, Any]] | list[SourceItem],
) -> str:
    return _render(
        "bootstrap.txt",
        stance_rubric=STANCE_RUBRIC,
        stance_format_rules=STANCE_FORMAT_RULES,
        customer=compact_customer_block(customer),
        items=compact_items_block(items, text_limit=700),
    )


def type_triage_prompt(
    customer: Customer,
    event: LinkedEvent,
    items: list[dict[str, Any]] | list[SourceItem],
) -> str:
    return _render(
        "type_triage.txt",
        customer=triage_customer_block(customer),
        event=triage_event_block(event),
        items=triage_items_block(items),
        stance_type_guide=triage_stance_type_guide(),
        tie_break=STANCE_TYPE_TIE_BREAK,
    )


def stance_tagging_prompt(
    customer: Customer,
    event: LinkedEvent,
    items: list[dict[str, Any]] | list[SourceItem],
    stance_catalog: list[dict[str, Any]],
    *,
    stance_type: StanceType | None = None,
    triage_hints: list[TypeTriageItem] | list[dict[str, Any]] | None = None,
    allow_add_proposals: bool = True,
) -> str:
    candidate_rule = (
        "Sólo los items listados en Hints son candidatos a recibir assignment."
        if triage_hints is not None
        else "No hay hints de triage en esta llamada; todos los Items son candidatos."
    )
    hints = [
        hint.to_dict() if isinstance(hint, TypeTriageItem) else hint
        for hint in triage_hints or []
    ]
    type_label = stance_type or "todas"
    type_example = stance_type or "entity_stance|complaint|gratefulness|suggestion|request|denuncia|question|endorsement|noise"
    proposal_rule = (
        "- Si una idea reusable de este tipo falta en el catálogo, puedes proponerla en proposals."
        if allow_add_proposals
        else "- No propongas add para este tipo en streaming; si no encaja en el catálogo, emite stance_id null."
    )
    return _render(
        "stance_tagging.txt",
        type_label=type_label,
        type_example=type_example,
        stance_type_guide=stance_type_guide(stance_type),
        customer=compact_customer_block(customer),
        event=compact_event_block(event),
        stance_catalog=stance_catalog_block(stance_catalog),
        triage_hints=json.dumps(hints, ensure_ascii=False, indent=2, default=json_default),
        candidate_rule=candidate_rule,
        items=compact_items_block(items),
        proposal_rule=proposal_rule,
        stance_format_rules=STANCE_FORMAT_RULES,
    )


def stance_update_prompt(
    customer: Customer,
    stance_catalog: list[dict[str, Any]],
    proposals: list[dict[str, Any]],
    sample_items: list[dict[str, Any]] | list[SourceItem],
) -> str:
    return _render(
        "stance_update.txt",
        stance_rubric=STANCE_RUBRIC,
        stance_type_guide=stance_type_guide(),
        stance_format_rules=STANCE_FORMAT_RULES,
        customer=compact_customer_block(customer),
        stance_catalog=stance_catalog_block(stance_catalog),
        proposals=indexed_block(proposals, "proposal_index"),
        sample_items=compact_items_block(sample_items, text_limit=700),
    )


def consistency_pass_prompt(
    customer: Customer,
    stance_catalog: list[dict[str, Any]],
    assignment_samples: list[dict[str, Any]],
    item_samples: list[dict[str, Any]],
    claim_summaries: list[dict[str, Any]],
) -> str:
    return _render(
        "consistency_pass.txt",
        customer=compact_customer_block(customer),
        stance_type_guide=stance_type_guide(),
        stance_catalog=stance_catalog_block(stance_catalog),
        assignment_samples=json.dumps(
            assignment_samples, ensure_ascii=False, indent=2, default=json_default
        ),
        item_samples=json.dumps(item_samples, ensure_ascii=False, indent=2, default=json_default),
        claim_summaries=json.dumps(
            claim_summaries, ensure_ascii=False, indent=2, default=json_default
        ),
    )


def claim_tagging_prompt(
    customer: Customer,
    event: LinkedEvent,
    items: list[dict[str, Any]] | list[SourceItem],
    *,
    include_comments: bool = False,
) -> str:
    items_rule = (
        "- Los Items pueden ser de kind article, user_post o user_comment."
        if include_comments
        else "- Los Items deben ser únicamente de kind article o user_post; "
             "si aparece un user_comment, ignóralo."
    )
    return _render(
        "claim_tagging.txt",
        claim_extraction_rules=_claim_extraction_rules(include_comments),
        customer=compact_customer_block(customer, include_entity_id=True),
        event=compact_event_block(event),
        items=compact_items_block(items),
        items_rule=items_rule,
    )


def claim_update_prompt(
    customer: Customer,
    event: LinkedEvent,
    claims: list[dict[str, Any]],
    clusters: list[dict[str, Any]],
) -> str:
    return _render(
        "claim_update.txt",
        claim_cluster_rules=CLAIM_CLUSTER_RULES,
        customer=compact_customer_block(customer),
        event=compact_event_block(event),
        clusters=claim_catalog_block(clusters),
        claims=indexed_block(claims, "claim_index"),
    )
