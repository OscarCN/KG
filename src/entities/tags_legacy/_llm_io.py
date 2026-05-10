"""Shared LLM call helpers for the tags subsystem.

Mirrors the pattern in `src/entities/linking/link_llm.py`:
- sha256 cache key over a stable canonical-JSON payload,
- 3-attempt retry,
- JSON-mode response_format,
- file cache under `cache/tags_<phase>/<sha256>.json`.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import time
from datetime import date, datetime
from pathlib import Path
from typing import Any, Optional

from src.llm.openrouter import call_openrouter

logger = logging.getLogger(__name__)


_PROJECT_ROOT = Path(__file__).resolve().parents[3]
_PROMPTS_DIR = Path(__file__).resolve().parent / "prompts"


def _json_default(value: Any) -> Any:
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    return str(value)


def render_prompt(template: str, **fields: Any) -> str:
    """Substitute `{name}` placeholders in `template` from `fields`.

    Uses straight `str.replace` so JSON examples in the template can use
    literal `{` and `}` without escaping.
    """
    out = template
    for k, v in fields.items():
        out = out.replace("{" + k + "}", str(v))
    return out


def load_prompt(name: str) -> str:
    """Load a prompt template by stem (e.g. `"tagging"` → `prompts/tagging.txt`)."""
    return (_PROMPTS_DIR / f"{name}.txt").read_text(encoding="utf-8")


def cache_dir_for(phase: str, customer_id: int) -> Path:
    return _PROJECT_ROOT / "cache" / f"tags_{phase}" / f"customer_{customer_id}"


def payload_key(payload: dict) -> str:
    blob = json.dumps(payload, sort_keys=True, ensure_ascii=False, default=_json_default)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def cache_read(cache_dir: Path, key: str) -> Optional[dict]:
    path = cache_dir / f"{key}.json"
    if not path.exists():
        return None
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return None


def cache_write(cache_dir: Path, key: str, value: dict) -> None:
    cache_dir.mkdir(parents=True, exist_ok=True)
    path = cache_dir / f"{key}.json"
    with open(path, "w", encoding="utf-8") as f:
        json.dump(value, f, ensure_ascii=False, default=_json_default)


def parse_json_response(raw: str) -> Optional[dict]:
    text = (raw or "").strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        logger.warning("Could not JSON-parse LLM response: %r", text[:200])
        return None


def call_with_retry(
    messages: list[dict],
    model: str,
    *,
    temperature: float = 0.0,
    max_attempts: int = 3,
    backoff_seconds: float = 2.0,
) -> Optional[str]:
    """Call OpenRouter with retry. Returns raw response text or None."""
    for attempt in range(1, max_attempts + 1):
        try:
            return call_openrouter(
                messages,
                model=model,
                response_format={"type": "json_object"},
                temperature=temperature,
            )
        except Exception as ex:
            logger.warning(
                "LLM call failed (attempt %d/%d, model=%s): %s",
                attempt,
                max_attempts,
                model,
                ex,
            )
            if attempt < max_attempts:
                time.sleep(backoff_seconds * attempt)
    return None


def call_cached(
    *,
    phase: str,
    customer_id: int,
    payload: dict,
    messages: list[dict],
    model: str,
    use_cache: bool = True,
) -> Optional[dict]:
    """Top-level helper: hash the payload, hit the cache, or call the LLM
    + cache the result. Returns the parsed JSON dict or None on failure.
    """
    cdir = cache_dir_for(phase, customer_id)
    key = payload_key({"model": model, **payload})
    if use_cache:
        cached = cache_read(cdir, key)
        if cached is not None:
            parsed = parse_json_response(cached.get("response", ""))
            if parsed is not None:
                logger.debug("[%s] cache hit (key=%s)", phase, key[:12])
                return parsed
            # cache had a response but it was unparseable — fall through to re-call

    raw = call_with_retry(messages, model)
    if raw is None:
        return None
    if use_cache:
        cache_write(cdir, key, {"response": raw, "model": model})
    return parse_json_response(raw)


def customer_context_block(customer) -> str:
    """Serialise the customer for inclusion in LLM prompts. Same shape
    used by all four phases so prompts can be rendered uniformly.
    """
    payload = {
        "entity_id": customer.entity_id,
        "name": customer.name,
        "description": customer.description,
        "aliases": list(customer.aliases),
        "types": [
            {"entity_type": t.entity_type, "entity_kind": t.entity_kind}
            for t in customer.types
        ],
    }
    return json.dumps(payload, ensure_ascii=False, indent=2)
