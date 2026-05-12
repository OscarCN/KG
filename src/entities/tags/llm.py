"""LLM helpers for the tags subsystem.

Adapted from `tags_legacy/_llm_io.py`. Provides:

- `JsonLlm` Protocol — uniform `call(prompt) -> dict` interface.
- `OpenRouterJsonLlm` — wraps `call_openrouter` with JSON-mode + retry.
- `CachedJsonLlm` — sha256(canonical-payload) → file cache under
  `cache/tags_<phase>/customer_<id>/<sha256>.json`.
- helpers: `payload_key`, `cache_dir_for`, `parse_json_response`,
  `render_prompt`, `load_prompt`.

The cache key composition includes the model name and any
`payload_key_extra` the caller wants to pin (catalog snapshot, prompt name,
items payload, etc.).
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import time
from pathlib import Path
from typing import Any, Optional, Protocol

from src.entities.tags.models import json_default
from src.llm.openrouter import call_openrouter


logger = logging.getLogger(__name__)


_PROJECT_ROOT = Path(__file__).resolve().parents[3]
_PROMPTS_DIR = Path(__file__).resolve().parent / "prompts"


# ── Prompt loading + rendering ──────────────────────────────────────────


def render_prompt(template: str, **fields: Any) -> str:
    """Substitute `{name}` placeholders by `str.replace`.

    JSON examples in the template can keep literal `{` / `}` without
    escaping; only `{registered_field}` substrings are replaced.
    """
    out = template
    for k, v in fields.items():
        out = out.replace("{" + k + "}", str(v))
    return out


def load_prompt(name: str) -> str:
    """Load `prompts/<name>.txt`. Sub-paths supported (e.g. `types/complaint`)."""
    return (_PROMPTS_DIR / f"{name}.txt").read_text(encoding="utf-8")


# ── Cache helpers ───────────────────────────────────────────────────────


def cache_dir_for(phase: str, customer_id: int) -> Path:
    return _PROJECT_ROOT / "cache" / f"tags_{phase}" / f"customer_{customer_id}"


def payload_key(payload: dict) -> str:
    blob = json.dumps(payload, sort_keys=True, ensure_ascii=False, default=json_default)
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
        json.dump(value, f, ensure_ascii=False, default=json_default)


# ── JSON parsing ────────────────────────────────────────────────────────


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


# ── JsonLlm protocol + adapters ────────────────────────────────────────


class JsonLlm(Protocol):
    """Uniform interface for the tags step classes."""

    def call(self, prompt: str, *, system: Optional[str] = None) -> dict: ...


class OpenRouterJsonLlm:
    """Wraps `call_openrouter` with JSON-mode + N-attempt retry."""

    def __init__(
        self,
        *,
        model: str,
        temperature: float = 0.0,
        retries: int = 3,
        backoff_seconds: float = 2.0,
    ):
        self.model = model
        self.temperature = temperature
        self.retries = retries
        self.backoff_seconds = backoff_seconds

    def call(self, prompt: str, *, system: Optional[str] = None) -> dict:
        messages: list[dict] = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": prompt})
        last_exc: Optional[Exception] = None
        last_raw: Optional[str] = None
        for attempt in range(1, self.retries + 1):
            try:
                raw = call_openrouter(
                    messages,
                    model=self.model,
                    response_format={"type": "json_object"},
                    temperature=self.temperature,
                )
                last_raw = raw
                parsed = parse_json_response(raw)
                if parsed is not None:
                    return parsed
                logger.warning(
                    "LLM returned unparseable JSON (attempt %d/%d, model=%s)",
                    attempt,
                    self.retries,
                    self.model,
                )
            except Exception as ex:
                last_exc = ex
                logger.warning(
                    "LLM call failed (attempt %d/%d, model=%s): %s",
                    attempt,
                    self.retries,
                    self.model,
                    ex,
                )
            if attempt < self.retries:
                time.sleep(self.backoff_seconds * attempt)
        # All retries exhausted — surface the full context and raise.
        self._raise_failure(messages, last_raw, last_exc)
        return {}  # unreachable; satisfies the type-checker

    def _raise_failure(
        self,
        messages: list[dict],
        last_raw: Optional[str],
        last_exc: Optional[Exception],
    ) -> None:
        """Dump the prompt + last raw output to stderr, then raise."""
        import sys as _sys

        border = "=" * 72
        print(
            f"\n{border}\n"
            f"LLM call FAILED after {self.retries} attempts "
            f"(model={self.model}, temperature={self.temperature})\n"
            f"{border}",
            file=_sys.stderr,
        )
        print(f"\n--- prompt messages ({len(messages)}) ---", file=_sys.stderr)
        for m in messages:
            print(f"\n[role: {m['role']}]", file=_sys.stderr)
            print(m["content"], file=_sys.stderr)
        if last_raw is not None:
            print(f"\n--- last raw response (length={len(last_raw)}) ---",
                  file=_sys.stderr)
            print(repr(last_raw) if not last_raw.strip() else last_raw,
                  file=_sys.stderr)
        else:
            print("\n--- no raw response captured (all attempts raised) ---",
                  file=_sys.stderr)
        if last_exc is not None:
            print(f"\n--- last exception ---\n{last_exc!r}", file=_sys.stderr)
        print(f"{border}\n", file=_sys.stderr)

        if last_exc is not None:
            raise RuntimeError(
                f"LLM call failed after {self.retries} attempts "
                f"(model={self.model}); see stderr dump above"
            ) from last_exc
        raise RuntimeError(
            f"LLM returned unparseable JSON after {self.retries} attempts "
            f"(model={self.model}); see stderr dump above"
        )


class LoggingJsonLlm:
    """Phase-tagged DEBUG-level dumps of every prompt and response.

    Logger names follow `tags.prompts.<phase>`. Six phases are wired in
    `run_tags.py` / `make_cached_openrouter`: `bootstrap`, `triage`,
    `tagging` (stance tagging), `claim_tag` (claim extraction),
    `claim_group` (claim clustering), `consistency`.

    Enable for one phase:
        logging.getLogger("tags.prompts.bootstrap").setLevel(logging.DEBUG)
    Enable for all phases:
        logging.getLogger("tags.prompts").setLevel(logging.DEBUG)

    Wraps `CachedJsonLlm` as the outermost adapter, so cache HITs are also
    logged (usually what you want when debugging the LLM-facing payload).
    """

    def __init__(self, inner: "JsonLlm", *, phase: str):
        self.inner = inner
        self.phase = phase
        self._log = logging.getLogger(f"tags.prompts.{phase}")

    def call(self, prompt: str, *, system: Optional[str] = None) -> dict:
        if self._log.isEnabledFor(logging.DEBUG):
            self._log.debug(
                "[%s] PROMPT (system=%s)\n%s",
                self.phase,
                "yes" if system else "no",
                prompt,
            )
        response = self.inner.call(prompt, system=system)
        if self._log.isEnabledFor(logging.DEBUG):
            self._log.debug(
                "[%s] RESPONSE\n%s",
                self.phase,
                json.dumps(response, ensure_ascii=False, indent=2, default=json_default),
            )
        return response


class CachedJsonLlm:
    """Wraps a `JsonLlm` with a file cache.

    The cache key is sha256 over a canonical-JSON payload that includes the
    model, the prompt itself, and any `extra` fields the caller wants to
    pin to the key (e.g. catalog snapshot, items list, phase name).
    """

    def __init__(
        self,
        inner: JsonLlm,
        *,
        cache_dir: Path,
        model: str,
        extra: Optional[dict] = None,
    ):
        self.inner = inner
        self.cache_dir = cache_dir
        self.model = model
        self.extra = dict(extra or {})

    def call(self, prompt: str, *, system: Optional[str] = None) -> dict:
        key = payload_key(
            {"model": self.model, "prompt": prompt, "system": system or "", **self.extra}
        )
        cached = cache_read(self.cache_dir, key)
        if cached is not None and "response" in cached:
            return cached["response"]
        result = self.inner.call(prompt, system=system)
        cache_write(self.cache_dir, key, {"response": result, "model": self.model})
        return result


class ScriptedJsonLlm:
    """Test-only LLM that returns a queue of pre-baked responses."""

    def __init__(self, responses: list[dict]):
        self._responses = list(responses)
        self.calls: list[tuple[str, Optional[str]]] = []

    def call(self, prompt: str, *, system: Optional[str] = None) -> dict:
        self.calls.append((prompt, system))
        if not self._responses:
            raise RuntimeError("ScriptedJsonLlm: no more responses queued")
        return self._responses.pop(0)


# ── Convenience factory ────────────────────────────────────────────────


def make_cached_openrouter(
    *,
    phase: str,
    customer_id: int,
    model: str,
    extra: Optional[dict] = None,
) -> JsonLlm:
    """Build the standard cached-OpenRouter LLM for a given phase.

    Layering (outer → inner): `LoggingJsonLlm` → `CachedJsonLlm` →
    `OpenRouterJsonLlm`. Set `tags.prompts.<phase>` to DEBUG to dump
    prompts and responses.
    """
    inner = OpenRouterJsonLlm(model=model)
    cache_dir = cache_dir_for(phase, customer_id)
    cached = CachedJsonLlm(inner, cache_dir=cache_dir, model=model, extra=extra)
    return LoggingJsonLlm(cached, phase=phase)
