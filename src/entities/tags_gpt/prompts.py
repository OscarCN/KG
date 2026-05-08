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


def indexed_block(values: list[dict[str, Any]], index_name: str) -> str:
    payload = []
    for index, value in enumerate(values):
        item = dict(value)
        item[index_name] = index
        payload.append(item)
    return json.dumps(payload, ensure_ascii=False, indent=2, default=json_default)


STANCE_RUBRIC = """
Una postura es una cualidad o comportamiento DURADERO que el público atribuye al cliente.
Una postura válida cumple tres condiciones:
- Reutilizable: puede aplicar a varios eventos futuros del cliente, no a un único incidente.
- Nivel intermedio: específica como cualidad o comportamiento, no genérica ni atada a un detalle coyuntural.
- Respaldada por evidencia: aparece en varias muestras o como patrón claro; no se basa en un solo comentario, sarcasmo, ironía o ruido.

Malas posturas:
- "la calle Juárez sigue rota" porque es específica de un incidente.
- "la aseguradora ignoró la reclamación 123" porque es un caso individual.
- "el ayuntamiento es malo" porque es demasiado vaga.
- "lol qué desastre" porque es ruido o sarcasmo sin patrón.

Buenas posturas:
- "el ayuntamiento es ineficiente"
- "el ayuntamiento descuida la infraestructura"
- "el alcalde es deshonesto"
- "la aseguradora retrasa pagos"
- "la empresa responde tarde a los reclamos"
""".strip()


STANCE_FORMAT_RULES = """
Reglas de calidad del catálogo:
- Mantén una lista corta, curada y reutilizable.
- Evita duplicados; si dos labels expresan la misma idea, usa una sola formulación.
- Las posturas opuestas pueden coexistir si ambas están sustentadas por evidencia.
- Escribe labels breves, neutrales y en español de México, idealmente de 5 a 12 palabras.
- Redacta cada label como cualidad o comportamiento del cliente, no como queja en primera persona.
- La description debe ser una sola oración que explique la cualidad duradera y no depender de un evento único.
""".strip()


_CLAIM_RULES_NO_COMMENTS = (
    "Extrae claims solo de artículos y publicaciones de usuarios. "
    "No extraigas claims de comentarios de usuarios (los comentarios suelen ser "
    "opinión, anécdota o sarcasmo, no reportaje fáctico)."
)
_CLAIM_RULES_INCLUDE_COMMENTS = (
    "Puedes extraer claims de artículos, publicaciones y comentarios; "
    "aplica los criterios fácticos por igual a todas las fuentes."
)


def _claim_extraction_rules(include_comments: bool) -> str:
    source_rule = (
        _CLAIM_RULES_INCLUDE_COMMENTS if include_comments else _CLAIM_RULES_NO_COMMENTS
    )
    return f"""
Una claim es una aseveración FÁCTICA específica sobre el evento que afecta al cliente.
Incluye una claim solo si:
- Enuncia un hecho concreto, no una opinión ni una generalidad.
- Aplica al evento filtrado.
- Afecta al cliente; affected_entity_ids debe incluir el entity_id del cliente.
- Tiene una frase textual representativa en verbatim, copiada desde la fuente.

{source_rule}
Descarta opiniones, afirmaciones vagas, sarcasmo, ironía, trolling, ruido y hechos que no afecten al cliente.
importance es 1=baja, 2=media, 3=alta según qué tan importante es que el cliente esté al tanto dentro del evento.
""".strip()


# Default value (no comments) preserved for callers that imported the constant.
CLAIM_EXTRACTION_RULES = _claim_extraction_rules(include_comments=False)


CLAIM_CLUSTER_RULES = """
Un buen cluster agrupa claims que expresan la MISMA alegación sobre el evento aunque usen palabras distintas.
La frase canonical debe decir qué se alega, con sujeto y predicado; no debe ser vaga ni sobre-especificada con detalles irrelevantes.

Decisiones por claim:
- assign: un cluster existente expresa la misma alegación. Si resumir la claim con la canonical del cluster no pierde información esencial, asigna.
- create: ningún cluster la cubre y la claim es sustantiva; crea una canonical específica.
- drop: la claim es vaga, fuera de tema, opinión, sarcasmo, ruido, trolling o duplicado puro dentro del lote.

Prioridad cuando dudes: assign > create > drop.
No crees un cluster nuevo si uno existente cubre razonablemente la claim. No descartes una claim sustantiva solo porque no encaja perfecto; crea un cluster.

Mutations opcionales:
- rename: mejora la canonical de un cluster existente considerando sus citas.
- merge: fusiona dos clusters solo si describen la misma alegación. Sé conservador; temas parecidos con alegaciones distintas deben coexistir.
""".strip()


def bootstrap_prompt(customer: Customer, items: list[SourceItem]) -> str:
    return f"""
Eres un analista que construye el catálogo inicial de posturas sobre una entidad cliente.
El catálogo se usará después para etiquetar contenido de forma consistente, así que debe ser corto, curado y reutilizable.

{STANCE_RUBRIC}

Proceso:
1. Lee el corpus como única fuente de evidencia.
2. Busca quejas, elogios, atribuciones y juicios dirigidos al cliente explícita o implícitamente.
3. Agrupa muestras que expresan la misma cualidad con palabras distintas.
4. Redacta una postura canónica por grupo.
5. Descarta grupos con una sola muestra aislada o atados a un único evento.

{STANCE_FORMAT_RULES}

Cliente:
{customer_block(customer)}

Corpus relevante:
{items_block(items, text_limit=700)}

Devuelve entre 5 y 15 posturas si el corpus lo permite. Si hay menos evidencia, devuelve menos antes de inventar.

Responde EXCLUSIVAMENTE con JSON válido:
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
Eres un sistema de etiquetado de posturas hacia el cliente.

{STANCE_RUBRIC}

Cliente:
{customer_block(customer)}

Evento usado como filtro, no como alcance del catálogo:
{event_block(event)}

Catálogo actual de posturas:
{stance_catalog_block(stance_catalog)}

Items:
{items_block(items)}

Reglas:
- Para cada item, asigna cero o una postura existente.
- Asigna una postura solo si aplica claramente al item.
- Usa solo stance_id que aparezcan en el catálogo actual.
- No inventes ids en assignments.
- Si detectas una idea recurrente necesaria que no está en el catálogo, proponla en proposals.
- Las ideas nuevas van en proposals, nunca en assignments.
- No propongas duplicados de algo que el catálogo ya cubre.
- El evento sirve como filtro/contexto, no como nivel de abstracción de la postura.
- Las posturas opuestas pueden coexistir si reflejan percepciones reales del público.

{STANCE_FORMAT_RULES}

Responde EXCLUSIVAMENTE con JSON válido:
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
Eres el adjudicador del catálogo de posturas del cliente.
Tu trabajo es proteger el catálogo de etiquetas demasiado específicas, demasiado vagas o duplicadas.

{STANCE_RUBRIC}

{STANCE_FORMAT_RULES}

Cliente:
{customer_block(customer)}

Catálogo actual:
{stance_catalog_block(stance_catalog)}

Propuestas a adjudicar, con proposal_index de base cero:
{indexed_block(proposals, "proposal_index")}

Muestras:
{items_block(sample_items, text_limit=700)}

Operaciones:
- accept: aplica la propuesta tal como vino; add crea una postura nueva y rename renombra src_stance_id.
- reject: descarta la propuesta; incluye reason con la condición que falló.
- generalise: solo para propuestas add; la idea ya está cubierta por existing_id y el catálogo no cambia.
- rename: usa una mejor formulación para una postura existente; requiere existing_id, new_label y new_description.

Heurística de decisión:
1. Primero verifica si la propuesta es una postura válida.
2. Si no es reutilizable, no tiene nivel intermedio o no está respaldada por evidencia, usa reject.
3. Si una entrada existente ya cubre la idea y la propuesta mejora la formulación, usa rename.
4. Si una entrada existente ya cubre la idea y la propuesta no mejora la formulación, usa generalise.
5. Usa accept solo cuando la propuesta es válida y no se solapa con ninguna entrada existente.
6. Para propuestas rename, usa accept si mejora el src_stance_id; usa reject si empeora o duplica otra entrada.

Cada propuesta debe tener exactamente una decisión.
existing_id debe existir en el catálogo actual cuando action sea rename o generalise.
generalise solo aplica a propuestas add.

Responde EXCLUSIVAMENTE con JSON válido:
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


def claim_tagging_prompt(
    customer: Customer,
    event: LinkedEvent,
    items: list[SourceItem],
    *,
    include_comments: bool = False,
) -> str:
    items_rule = (
        "- Los Items pueden ser de kind article, user_post o user_comment."
        if include_comments
        else "- Los Items deben ser únicamente de kind article o user_post; "
             "si aparece un user_comment, ignóralo."
    )
    return f"""
Eres un extractor de claims factuales para un evento específico.

{_claim_extraction_rules(include_comments)}

Cliente:
{customer_block(customer)}

Evento:
{event_block(event)}

Items:
{items_block(items)}

Reglas:
{items_rule}
- Usa cada source_item_id exactamente como aparece en Items.
- No inventes affected_entity_ids; incluye el entity_id del cliente si y solo si la claim lo afecta.
- verbatim debe ser una frase textual representativa tomada de la fuente.
- importance_reason debe ser una oración breve.
- Si no hay claims válidas, devuelve "claims": [].

Responde EXCLUSIVAMENTE con JSON válido:
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
Eres el agrupador de claims para un evento específico.

{CLAIM_CLUSTER_RULES}

Cliente:
{customer_block(customer)}

Evento:
{event_block(event)}

Clusters existentes:
{claim_catalog_block(clusters)}

Claims nuevos, con claim_index de base cero:
{indexed_block(claims, "claim_index")}

Reglas estrictas:
- Cada claim_index debe tener exactamente una decisión.
- cluster_id, src_id y dst_id deben existir en Clusters existentes.
- Para assign, cluster_id es obligatorio.
- Para create, canonical es obligatorio.
- Para drop, reason es obligatorio.
- mutations puede ser [].
- merge requiere src_id distinto de dst_id.

Responde EXCLUSIVAMENTE con JSON válido:
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
Decide si el evento entrante es la misma ocurrencia real que alguno de los candidatos.
Considera nombre, descripción, ubicación y fecha. Dos registros pueden ser el mismo evento aunque tengan nombres ligeramente distintos o descripciones complementarias.
Si describen hechos distintos, aunque compartan tipo, ciudad o fecha, no son el mismo evento.

Evento entrante:
{json.dumps(incoming, ensure_ascii=False, indent=2, default=json_default)}

Candidatos:
{json.dumps(candidates, ensure_ascii=False, indent=2, default=json_default)}

Devuelve solo un id que aparezca en Candidatos. Si ninguno coincide claramente, devuelve null.

Responde EXCLUSIVAMENTE con JSON válido:
{{"match_id": "<id de un candidato>"}} o {{"match_id": null}}
""".strip()
