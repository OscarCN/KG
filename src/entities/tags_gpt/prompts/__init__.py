"""Prompt builders for tags_gpt.

The text is intentionally compact and Spanish-first because source material
and labels are Mexico-focused Spanish content.
"""

from __future__ import annotations

import json
from typing import Any

from src.entities.tags_gpt.models import Customer, LinkedEventContext, StanceType


STANCE_TYPE_GUIDE = """
Tipos: entity_stance, complaint, gratefulness, suggestion, request, denuncia,
question, endorsement, noise.
Regla de desempate para una misma idea:
denuncia > request > complaint > suggestion > gratefulness > endorsement >
entity_stance > question > noise.
Un item puede tener varias ideas distintas; devuelve una fila por idea.
noise solo se usa cuando el item no tiene senal util sobre el cliente.
""".strip()


def _json_block(payload: dict[str, Any]) -> str:
    return json.dumps(payload, ensure_ascii=False, indent=2)


def type_triage_prompt(payload: dict[str, Any]) -> str:
    return (
        "Clasifica ideas de postura hacia el cliente. No elijas catalogos ni extraigas claims.\n"
        f"{STANCE_TYPE_GUIDE}\n"
        "Devuelve JSON: {\"triage\": [{\"source_item_id\": 1, \"stance_type\": \"complaint\", "
        "\"brief_summary\": \"...\", \"importance_hint\": \"low|medium|high\"}]}.\n\n"
        f"Entrada:\n{_json_block(payload)}"
    )


def stance_tagging_prompt(payload: dict[str, Any], stance_type: StanceType) -> str:
    return (
        f"Etiqueta solo ideas de tipo {stance_type}. Usa solo entradas del catalogo de ese tipo.\n"
        "Si ninguna entrada encaja, usa stance_id null. Puedes proponer add o rename.\n"
        "Devuelve JSON: {\"assignments\": [...], \"proposals\": [...]}.\n\n"
        f"Entrada:\n{_json_block(payload)}"
    )


def claim_tagging_prompt(payload: dict[str, Any]) -> str:
    return (
        "Extrae afirmaciones factuales sobre el evento que afectan al cliente fijo.\n"
        "No asignes clusters. Importancia: 1 baja, 2 media, 3 alta.\n"
        "Devuelve JSON: {\"claims\": [{\"source_item_id\": 1, \"verbatim\": \"...\", "
        "\"importance\": 1, \"importance_reason\": \"...\"}]}.\n\n"
        f"Entrada:\n{_json_block(payload)}"
    )


def claim_update_prompt(payload: dict[str, Any]) -> str:
    return (
        "Agrupa claims en clusters existentes o crea clusters nuevos. Puedes sugerir rename o merge.\n"
        "Usa claim_index local. Devuelve JSON: {\"decisions\": [...], \"mutations\": [...]}.\n\n"
        f"Entrada:\n{_json_block(payload)}"
    )


def bootstrap_catalog_prompt(
    customer: Customer,
    stance_type: StanceType,
    items: list[dict[str, Any]],
    *,
    min_evidence: int,
) -> str:
    payload = {
        "customer": {"name": customer.name, "description": customer.description},
        "stance_type": stance_type,
        "min_evidence": min_evidence,
        "items": items,
    }
    return (
        f"Crea un catalogo inicial de posturas tipo {stance_type} para el cliente.\n"
        "Cada entrada requiere label, description y evidence_source_item_ids.\n"
        "Devuelve JSON: {\"entries\": [...]}.\n\n"
        f"Entrada:\n{_json_block(payload)}"
    )


def consistency_prompt(payload: dict[str, Any], stance_type: StanceType) -> str:
    return (
        f"Consolida solo el catalogo de tipo {stance_type}. No fusiones tipos diferentes.\n"
        "Devuelve JSON con proposals, merge_pairs, retire_ids y reroute_pairs.\n\n"
        f"Entrada:\n{_json_block(payload)}"
    )


def compact_customer(customer: Customer, *, include_id: bool = False) -> dict[str, Any]:
    payload: dict[str, Any] = {"name": customer.name, "description": customer.description}
    if include_id:
        payload["entity_id"] = customer.entity_id
    return payload


def compact_event(event: LinkedEventContext | None) -> dict[str, str] | None:
    if not event:
        return None
    return {"description": event.description}

