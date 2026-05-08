"""LLM disambiguation for non-event entities.

Entity linking intentionally uses only name and description for now.
Candidate retrieval is handled outside this module.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
from datetime import date, datetime
from pathlib import Path
from typing import Any, Optional

from src.llm.openrouter import call_openrouter

logger = logging.getLogger(__name__)

_DEFAULT_MODEL = os.environ.get("OPENROUTER_ENTITY_LINKER_MODEL", "google/gemini-2.5-flash-lite")
_CACHE_DIR = Path(__file__).resolve().parents[3] / "cache" / "entity_link_llm"


def _json_default(value: Any) -> Any:
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    return str(value)


def _payload_key(incoming: dict[str, Any], candidates: list[dict[str, Any]]) -> str:
    payload = {"model": _DEFAULT_MODEL, "incoming": incoming, "candidates": candidates}
    blob = json.dumps(payload, sort_keys=True, ensure_ascii=False, default=_json_default)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def _cache_read(key: str) -> Optional[dict[str, Any]]:
    path = _CACHE_DIR / f"{key}.json"
    if not path.exists():
        return None
    try:
        with open(path, encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, json.JSONDecodeError):
        return None


def _cache_write(key: str, value: dict[str, Any]) -> None:
    _CACHE_DIR.mkdir(parents=True, exist_ok=True)
    path = _CACHE_DIR / f"{key}.json"
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(value, handle, ensure_ascii=False, default=_json_default)


_SYSTEM_PROMPT = (
    "Eres un modelo de desambiguacion de entidades. Recibes una entidad "
    "entrante y una lista de entidades candidatas ya registradas. Decide si "
    "la entidad entrante es la MISMA entidad/concepto que alguno de los "
    "candidatos. Usa solo nombre y descripcion. Responde exclusivamente con "
    'JSON: {"match_id": "<id de un candidato>"} o {"match_id": null}. '
    "Solo puedes devolver un id que aparezca en candidatos."
)


def _user_message(incoming: dict[str, Any], candidates: list[dict[str, Any]]) -> str:
    return "\n".join(
        [
            "Entidad entrante:",
            json.dumps(incoming, ensure_ascii=False, indent=2, default=_json_default),
            "",
            f"Candidatos ({len(candidates)}):",
            json.dumps(candidates, ensure_ascii=False, indent=2, default=_json_default),
            "",
            'Responde con {"match_id": "<id>"} o {"match_id": null}.',
        ]
    )


def _parse_response(raw: str, candidate_ids: set[str]) -> Optional[str]:
    text = (raw or "").strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        logger.warning("Could not JSON-parse entity linker response: %r", raw[:200])
        return None
    if not isinstance(parsed, dict):
        return None
    match_id = parsed.get("match_id")
    if match_id is None:
        return None
    if isinstance(match_id, str) and match_id in candidate_ids:
        return match_id
    logger.warning("Entity linker returned unknown match_id %r", match_id)
    return None


def disambiguate_entity(
    incoming: dict[str, Any],
    candidates: list[dict[str, Any]],
    use_cache: bool = True,
) -> Optional[str]:
    if not candidates:
        return None
    candidate_ids = {candidate["id"] for candidate in candidates if candidate.get("id")}
    if not candidate_ids:
        return None

    cache_key = _payload_key(incoming, candidates)
    if use_cache:
        cached = _cache_read(cache_key)
        if cached is not None:
            return _parse_response(cached.get("response", ""), candidate_ids)

    try:
        raw = call_openrouter(
            [
                {"role": "system", "content": _SYSTEM_PROMPT},
                {"role": "user", "content": _user_message(incoming, candidates)},
            ],
            model=_DEFAULT_MODEL,
            response_format={"type": "json_object"},
            temperature=0.0,
        )
    except Exception as ex:
        logger.warning("Entity linker LLM call failed: %s", ex)
        return None

    if use_cache:
        _cache_write(cache_key, {"response": raw, "model": _DEFAULT_MODEL})
    return _parse_response(raw, candidate_ids)
