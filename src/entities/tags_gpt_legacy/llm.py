"""Injectable JSON LLM clients.

Pipeline steps depend on the `JsonLlm` protocol, which makes them easy
to test with scripted responses and easy to move into separate services.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import time
from pathlib import Path
from typing import Any, Callable, Optional, Protocol

from src.entities.tags_gpt.models import json_default

logger = logging.getLogger(__name__)
llm_io_logger = logging.getLogger("src.entities.tags_gpt.llm_io")


class JsonLlm(Protocol):
    def complete_json(
        self,
        *,
        phase: str,
        payload: dict[str, Any],
        prompt: str,
        model: str,
    ) -> dict[str, Any]: ...


def parse_json_response(raw: str) -> dict[str, Any]:
    text = (raw or "").strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ValueError(f"LLM response was not JSON: {text[:200]!r}") from exc
    if not isinstance(parsed, dict):
        raise ValueError("LLM response JSON must be an object")
    return parsed


class OpenRouterJsonLlm:
    def __init__(self, *, temperature: float = 0.0, max_attempts: int = 3):
        self.temperature = temperature
        self.max_attempts = max_attempts

    def complete_json(
        self,
        *,
        phase: str,
        payload: dict[str, Any],
        prompt: str,
        model: str,
    ) -> dict[str, Any]:
        from src.llm.openrouter import call_openrouter

        messages = [{"role": "user", "content": prompt}]
        for attempt in range(1, self.max_attempts + 1):
            try:
                raw = call_openrouter(
                    messages,
                    model=model,
                    response_format={"type": "json_object"},
                    temperature=self.temperature,
                )
                return parse_json_response(raw)
            except Exception:
                if attempt == self.max_attempts:
                    raise
                logger.warning("LLM phase %s failed on attempt %s", phase, attempt)
                time.sleep(attempt * 2)
        return {}


class CachedJsonLlm:
    def __init__(self, inner: JsonLlm, cache_root: Path):
        self.inner = inner
        self.cache_root = cache_root

    def complete_json(
        self,
        *,
        phase: str,
        payload: dict[str, Any],
        prompt: str,
        model: str,
    ) -> dict[str, Any]:
        key = self._key({"phase": phase, "model": model, "payload": payload, "prompt": prompt})
        path = self.cache_root / phase / f"{key}.json"
        if path.exists():
            with open(path, encoding="utf-8") as handle:
                cached = json.load(handle)
            response = dict(cached["response"])
            _log_llm_io(
                phase=phase,
                model=model,
                prompt=prompt,
                response=response,
                cache_hit=True,
            )
            return response
        response = self.inner.complete_json(phase=phase, payload=payload, prompt=prompt, model=model)
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w", encoding="utf-8") as handle:
            json.dump({"response": response}, handle, ensure_ascii=False, indent=2, default=json_default)
        _log_llm_io(
            phase=phase,
            model=model,
            prompt=prompt,
            response=response,
            cache_hit=False,
        )
        return response

    @staticmethod
    def _key(payload: dict[str, Any]) -> str:
        blob = json.dumps(payload, sort_keys=True, ensure_ascii=False, default=json_default)
        return hashlib.sha256(blob.encode("utf-8")).hexdigest()


class ScriptedJsonLlm:
    """Test helper.

    `responses` may map a phase to a dict, to a list of dicts consumed in
    order, or to a callable receiving `(payload, prompt, model)`.
    """

    def __init__(
        self,
        responses: dict[
            str,
            dict[str, Any]
            | list[dict[str, Any]]
            | Callable[[dict[str, Any], str, str], dict[str, Any]],
        ],
    ):
        self.responses = dict(responses)
        self.calls: list[dict[str, Any]] = []

    def complete_json(
        self,
        *,
        phase: str,
        payload: dict[str, Any],
        prompt: str,
        model: str,
    ) -> dict[str, Any]:
        self.calls.append({"phase": phase, "payload": payload, "prompt": prompt, "model": model})
        response = self.responses.get(phase)
        if response is None:
            parsed_response: dict[str, Any] = {}
            _log_llm_io(
                phase=phase,
                model=model,
                prompt=prompt,
                response=parsed_response,
                cache_hit=None,
            )
            return parsed_response
        if callable(response):
            parsed_response = dict(response(payload, prompt, model))
        elif isinstance(response, list):
            if not response:
                parsed_response = {}
            else:
                parsed_response = dict(response.pop(0))
        else:
            parsed_response = dict(response)
        _log_llm_io(
            phase=phase,
            model=model,
            prompt=prompt,
            response=parsed_response,
            cache_hit=None,
        )
        return parsed_response


def default_cached_llm(cache_root: Optional[Path] = None) -> JsonLlm:
    root = cache_root or Path(__file__).resolve().parents[3] / "cache" / "tags_gpt"
    return CachedJsonLlm(OpenRouterJsonLlm(), root)


def _log_llm_io(
    *,
    phase: str,
    model: str,
    prompt: str,
    response: dict[str, Any],
    cache_hit: Optional[bool],
) -> None:
    if not llm_io_logger.isEnabledFor(logging.DEBUG):
        return
    cache_label = "n/a" if cache_hit is None else ("hit" if cache_hit else "miss")
    llm_io_logger.debug(
        "LLM call phase=%s model=%s cache=%s\nPROMPT:\n%s\nRESPONSE:\n%s",
        phase,
        model,
        cache_label,
        prompt,
        json.dumps(response, ensure_ascii=False, indent=2, default=json_default),
    )
