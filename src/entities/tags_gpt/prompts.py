"""Prompt builders for the decoupled tags steps."""

from __future__ import annotations

import json
from typing import Any

from src.entities.tags_gpt.models import Customer, LinkedEvent, SourceItem, json_default


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


def stance_catalog_block(entries: list[dict[str, Any]]) -> str:
    return json.dumps(entries, ensure_ascii=False, indent=2, default=json_default)


def claim_catalog_block(clusters: list[dict[str, Any]]) -> str:
    return json.dumps(clusters, ensure_ascii=False, indent=2, default=json_default)


def bootstrap_prompt(customer: Customer, items: list[SourceItem]) -> str:
    return f"""
Eres un analista de reputación pública.

Cliente:
{customer_block(customer)}

Corpus relevante:
{items_block(items, text_limit=700)}

Construye un catálogo inicial de posturas duraderas hacia el cliente.
Las posturas deben ser cualidades o comportamientos atribuidos al cliente,
no quejas específicas de un solo evento.

Responde SOLO JSON:
{{
  "stances": [
    {{"label": "...", "description": "..."}}
  ]
}}
""".strip()


def stance_tagging_prompt(
    customer: Customer,
    event: LinkedEvent,
    items: list[SourceItem],
    stance_catalog: list[dict[str, Any]],
) -> str:
    return f"""
Etiqueta posturas hacia el cliente.

Cliente:
{customer_block(customer)}

Evento usado como filtro, no como alcance del catálogo:
{event_block(event)}

Catálogo actual de posturas:
{stance_catalog_block(stance_catalog)}

Items:
{items_block(items)}

Para cada item, asigna cero o una postura existente por id. Si una postura
necesaria no existe, proponla en "proposals"; no inventes ids en assignments.

Responde SOLO JSON:
{{
  "assignments": [
    {{"source_item_id": "...", "stance_id": "...", "reason": "..."}}
  ],
  "proposals": [
    {{"kind": "add", "label": "...", "description": "...", "source_item_ids": ["..."]}},
    {{"kind": "rename", "src_stance_id": "...", "label": "...", "description": "..."}}
  ]
}}
""".strip()


def stance_update_prompt(
    customer: Customer,
    stance_catalog: list[dict[str, Any]],
    proposals: list[dict[str, Any]],
    sample_items: list[SourceItem],
) -> str:
    return f"""
Evalúa cambios propuestos al catálogo de posturas del cliente.

Cliente:
{customer_block(customer)}

Catálogo actual:
{stance_catalog_block(stance_catalog)}

Propuestas:
{json.dumps(proposals, ensure_ascii=False, indent=2)}

Muestras:
{items_block(sample_items, text_limit=700)}

Decide por propuesta:
- accept: crear la nueva postura propuesta.
- reject: descartar.
- rename: renombrar una postura existente.
- generalise: usar una postura existente en lugar de crear otra.

Responde SOLO JSON:
{{
  "decisions": [
    {{
      "proposal_index": 0,
      "action": "accept|reject|rename|generalise",
      "existing_id": null,
      "new_label": null,
      "new_description": null,
      "reason": "..."
    }}
  ]
}}
""".strip()


def claim_tagging_prompt(customer: Customer, event: LinkedEvent, items: list[SourceItem]) -> str:
    return f"""
Extrae afirmaciones factuales específicas sobre el evento que afecten al cliente.

Cliente:
{customer_block(customer)}

Evento:
{event_block(event)}

Items:
{items_block(items)}

Conserva una afirmación solo si afecta al cliente; affected_entity_ids debe
incluir el entity_id del cliente. Cada claim debe conservar una frase textual
representativa en verbatim. importance es 1, 2 o 3 según relevancia para el
cliente.

Responde SOLO JSON:
{{
  "claims": [
    {{
      "source_item_id": "...",
      "affected_entity_ids": [123],
      "verbatim": "...",
      "importance": 1,
      "importance_reason": "..."
    }}
  ]
}}
""".strip()


def claim_update_prompt(
    customer: Customer,
    event: LinkedEvent,
    claims: list[dict[str, Any]],
    clusters: list[dict[str, Any]],
) -> str:
    return f"""
Actualiza el catálogo de claims del evento.

Cliente:
{customer_block(customer)}

Evento:
{event_block(event)}

Clusters existentes:
{claim_catalog_block(clusters)}

Claims nuevos:
{json.dumps(claims, ensure_ascii=False, indent=2, default=json_default)}

Para cada claim, decide:
- assign: va a un cluster existente.
- create: crea un cluster nuevo con canonical.
- drop: es demasiado vago, duplicado como ruido, o no afecta al cliente.

También puedes proponer mutations para rename o merge de clusters existentes.

Responde SOLO JSON:
{{
  "decisions": [
    {{"claim_index": 0, "action": "assign|create|drop", "cluster_id": null, "canonical": null, "reason": "..."}}
  ],
  "mutations": [
    {{"kind": "rename", "cluster_id": "...", "new_canonical": "..."}},
    {{"kind": "merge", "src_id": "...", "dst_id": "..."}}
  ]
}}
""".strip()


def link_prompt(incoming: dict[str, Any], candidates: list[dict[str, Any]]) -> str:
    return f"""
Decide si el evento entrante es el mismo evento que alguno de los candidatos.

Evento entrante:
{json.dumps(incoming, ensure_ascii=False, indent=2, default=json_default)}

Candidatos:
{json.dumps(candidates, ensure_ascii=False, indent=2, default=json_default)}

Responde SOLO JSON:
{{"match_id": "id-del-candidato-o-null"}}
""".strip()
