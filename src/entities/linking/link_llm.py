"""LLM disambiguation for event linking.

Given an incoming event payload and a list of already-linked candidate
events, ask an LLM (`google/gemini-2.5-flash-lite` via OpenRouter)
whether the incoming event is the same real-world occurrence as any
of the candidates. Returns the matching candidate id, or None.

Each event payload exposes ONLY the fields the LLM needs to judge
identity:

    {
        "name": str | None,
        "description": str,
        "address": {country, state, city, neighborhood, zone,
                    street, number, place_name},   # the structured Location
        "date": {"start": ISO-string | None,
                 "end":   ISO-string | None},
    }

Candidates additionally carry an "id" field. The LLM is instructed to
return either `{"match_id": "<one of the candidate ids>"}` or
`{"match_id": null}` and that response is parsed defensively — any
id not present in the candidate list is treated as `null`.

Responses are cached under `cache/link_llm/<sha256>.json`, keyed by a
canonical hash of the payload, so re-runs avoid re-billing.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
from datetime import date, datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from src.llm.openrouter import call_openrouter

logger = logging.getLogger(__name__)


_DEFAULT_MODEL = os.environ.get("OPENROUTER_LINKER_MODEL", "google/gemini-2.5-flash-lite")
_CACHE_DIR = Path(__file__).resolve().parents[3] / "cache" / "link_llm"


# ---------------------------------------------------------------------------
# Cache helpers
# ---------------------------------------------------------------------------


def _json_default(value: Any) -> Any:
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    return str(value)


def _payload_key(incoming: Dict[str, Any], candidates: List[Dict[str, Any]]) -> str:
    payload = {"model": _DEFAULT_MODEL, "incoming": incoming, "candidates": candidates}
    blob = json.dumps(payload, sort_keys=True, ensure_ascii=False, default=_json_default)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def _cache_read(key: str) -> Optional[Dict[str, Any]]:
    path = _CACHE_DIR / f"{key}.json"
    if not path.exists():
        return None
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return None


def _cache_write(key: str, value: Dict[str, Any]) -> None:
    _CACHE_DIR.mkdir(parents=True, exist_ok=True)
    path = _CACHE_DIR / f"{key}.json"
    with open(path, "w", encoding="utf-8") as f:
        json.dump(value, f, ensure_ascii=False, default=_json_default)


# ---------------------------------------------------------------------------
# Prompt construction
# ---------------------------------------------------------------------------


_SYSTEM_PROMPT = (
    "Eres un modelo de desambiguación de eventos. Recibes un evento entrante "
    "y una lista de eventos candidatos ya registrados. Decide si el evento "
    "entrante es la MISMA ocurrencia real-world que alguno de los candidatos.\n\n"
    "Considera nombre, descripción, dirección estructurada y fechas. Dos "
    "registros pueden referirse al mismo evento aunque tengan nombres ligeramente "
    "distintos, descripciones complementarias o direcciones a diferentes niveles "
    "de detalle, siempre y cuando se trate del mismo hecho concreto en el mismo "
    "lugar y tiempo. Si las descripciones describen hechos distintos (aunque "
    "compartan tipo, fecha y ciudad), NO son el mismo evento.\n\n"
    "Responde EXCLUSIVAMENTE con un JSON con la forma:\n"
    '{\"match_id\": \"<id de un candidato>\"}  o  {\"match_id\": null}\n\n'
    "Solo puedes devolver un id que aparezca en la lista de candidatos. Si "
    "ninguno coincide claramente, devuelve null."
)


def _build_user_message(incoming: Dict[str, Any], candidates: List[Dict[str, Any]]) -> str:
    lines: List[str] = []
    lines.append("Evento entrante:")
    lines.append(json.dumps(incoming, ensure_ascii=False, indent=2, default=_json_default))
    lines.append("")
    lines.append(f"Candidatos ({len(candidates)}):")
    lines.append(json.dumps(candidates, ensure_ascii=False, indent=2, default=_json_default))
    lines.append("")
    lines.append(
        'Responde con {"match_id": "<id>"} si el evento entrante es el mismo '
        'que alguno de los candidatos, o {"match_id": null} si ninguno coincide.'
    )
    return "\n".join(lines)


def _parse_response(raw: str, candidate_ids: set[str]) -> Optional[str]:
    """Parse the LLM response defensively. Returns a candidate id or None."""
    text = (raw or "").strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        logger.warning("Could not JSON-parse linker response: %r", raw[:200])
        return None
    if not isinstance(parsed, dict):
        return None
    match_id = parsed.get("match_id")
    if match_id is None:
        return None
    if isinstance(match_id, str) and match_id in candidate_ids:
        return match_id
    logger.warning("Linker returned unknown match_id %r (candidates=%s)", match_id, candidate_ids)
    return None


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def disambiguate(
    incoming: Dict[str, Any],
    candidates: List[Dict[str, Any]],
    use_cache: bool = True,
) -> Optional[str]:
    """Decide whether `incoming` matches any of `candidates` via an LLM call.

    Returns the matching candidate's `id`, or `None` if no candidate matches.
    Empty `candidates` short-circuits to `None` without an LLM call.
    """
    if not candidates:
        return None

    candidate_ids = {c["id"] for c in candidates if c.get("id")}
    if not candidate_ids:
        return None

    cache_key = _payload_key(incoming, candidates)
    if use_cache:
        cached = _cache_read(cache_key)
        if cached is not None:
            match_id = _parse_response(cached.get("response", ""), candidate_ids)
            logger.debug(
                "Linker cache hit (key=%s): incoming=%r candidates=%s → match_id=%s",
                cache_key[:12],
                (incoming.get("name") or incoming.get("description", ""))[:80],
                [c["id"] for c in candidates],
                match_id,
            )
            return match_id

    user_message = _build_user_message(incoming, candidates)
    messages = [
        {"role": "system", "content": _SYSTEM_PROMPT},
        {"role": "user", "content": user_message},
    ]
    logger.debug(
        "Linker prompt (model=%s, %d candidates):\n--- SYSTEM ---\n%s\n--- USER ---\n%s",
        _DEFAULT_MODEL,
        len(candidates),
        _SYSTEM_PROMPT,
        user_message,
    )

    try:
        raw = call_openrouter(
            messages,
            model=_DEFAULT_MODEL,
            response_format={"type": "json_object"},
            temperature=0.0,
        )
    except Exception as ex:
        logger.warning("Linker LLM call failed: %s", ex)
        return None

    logger.debug("Linker raw response: %s", raw)

    if use_cache:
        _cache_write(cache_key, {"response": raw, "model": _DEFAULT_MODEL})

    match_id = _parse_response(raw, candidate_ids)
    logger.debug("Linker parsed match_id=%s", match_id)
    return match_id
