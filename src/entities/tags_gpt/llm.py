"""Injectable JSON LLM clients for tags_gpt."""

from __future__ import annotations

import hashlib
import json
import re
import time
from pathlib import Path
from typing import Any, Callable, Protocol

from src.entities.tags_gpt.models import json_default


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
    parsed = json.loads(text)
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
                if attempt >= self.max_attempts:
                    raise
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
            return dict(cached["response"])
        response = self.inner.complete_json(phase=phase, payload=payload, prompt=prompt, model=model)
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w", encoding="utf-8") as handle:
            json.dump({"response": response}, handle, ensure_ascii=False, indent=2, default=json_default)
        return response

    @staticmethod
    def _key(payload: dict[str, Any]) -> str:
        blob = json.dumps(payload, sort_keys=True, ensure_ascii=False, default=json_default)
        return hashlib.sha256(blob.encode("utf-8")).hexdigest()


class ScriptedJsonLlm:
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
            return {}
        if callable(response):
            return dict(response(payload, prompt, model))
        if isinstance(response, list):
            return dict(response.pop(0)) if response else {}
        return dict(response)


def default_cached_llm(cache_root: Path | None = None) -> JsonLlm:
    root = cache_root or Path.cwd() / "cache" / "tags_gpt"
    return CachedJsonLlm(OpenRouterJsonLlm(), root)

