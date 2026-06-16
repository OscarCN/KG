"""
OpenRouter LLM client.

Wraps the OpenRouter API (https://openrouter.ai/docs) using the
OpenAI-compatible chat completions endpoint.

Configuration via environment variables:
    OPENROUTER_API_KEY  — required
    OPENROUTER_MODEL    — optional, defaults to "openai/gpt-4o"

Usage:
    from src.llm.openrouter import call_openrouter

    response = call_openrouter([
        {"role": "system", "content": "You are a helpful assistant."},
        {"role": "user", "content": "Hello!"},
    ])
"""

from __future__ import annotations

import json
import os
import signal
import threading
from typing import Any, Dict, List, Optional

import requests


_API_URL = "https://openrouter.ai/api/v1/chat/completions"
_DEFAULT_MODEL = "openai/gpt-4o"

# Timeouts. ``requests``' own socket timeout has been observed not to fire when a
# network drop leaves the TLS connection half-open (the read parks forever in a
# C-level ``poll`` inside the SSL layer), so we layer two defences:
#   * an explicit (connect, read) tuple on the request — the read value caps the
#     silence allowed *between* bytes, kept generous so a slow model that holds
#     the connection quiet while generating is not killed prematurely;
#   * a hard wall-clock backstop via ``SIGALRM`` — a process-level timer that
#     interrupts even a blocked SSL read (a signal unwinds the syscall and the
#     handler raises), so the caller's retry logic can recover on a fresh
#     connection. SIGALRM only works on the main thread of a Unix process; off
#     the main thread (or where SIGALRM is unavailable) we fall back to the
#     socket timeout alone. An earlier watchdog-thread approach leaked a stuck
#     thread per drop and stopped firing once they piled up — the signal does
#     not depend on thread/executor state.
_CONNECT_TIMEOUT = 15      # seconds to establish the TCP/TLS connection
_READ_TIMEOUT = 150        # max seconds of silence between received bytes
_HARD_TIMEOUT = 180        # wall-clock backstop for a half-open socket


class OpenRouterClient:
    """Thin client for OpenRouter's chat completions endpoint."""

    def __init__(
        self,
        api_key: Optional[str] = None,
        model: Optional[str] = None,
        temperature: float = 0.0,
        max_tokens: int = 4096,
    ):
        self.api_key = api_key or os.environ.get("OPENROUTER_API_KEY")
        if not self.api_key:
            raise ValueError(
                "OpenRouter API key is required. Set OPENROUTER_API_KEY "
                "environment variable or pass api_key to the constructor."
            )
        self.model = model or os.environ.get("OPENROUTER_MODEL", _DEFAULT_MODEL)
        self.temperature = temperature
        self.max_tokens = max_tokens

    def chat(
        self,
        messages: List[Dict[str, str]],
        *,
        model: Optional[str] = None,
        temperature: Optional[float] = None,
        max_tokens: Optional[int] = None,
        response_format: Optional[Dict[str, Any]] = None,
    ) -> str:
        """Send a chat completion request and return the response content.

        Args:
            messages: list of {"role": "system"|"user"|"assistant", "content": "..."}
            model: override the default model for this call.
            temperature: override the default temperature.
            max_tokens: override the default max_tokens.
            response_format: optional, e.g. {"type": "json_object"} for JSON mode.

        Returns:
            The text content of the first choice in the response.

        Raises:
            requests.HTTPError: on non-2xx responses from OpenRouter.
            ValueError: if the response body is missing expected fields.
        """
        payload: Dict[str, Any] = {
            "model": model or self.model,
            "messages": messages,
            "temperature": temperature if temperature is not None else self.temperature,
            "max_tokens": max_tokens or self.max_tokens,
        }
        if response_format:
            payload["response_format"] = response_format

        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }

        def _do_request() -> Dict[str, Any]:
            resp = requests.post(
                _API_URL,
                headers=headers,
                json=payload,
                timeout=(_CONNECT_TIMEOUT, _READ_TIMEOUT),
            )
            resp.raise_for_status()
            return resp.json()

        # Hard wall-clock backstop. On the main thread, arm a SIGALRM timer that
        # interrupts a blocked SSL read (which the socket timeout has been seen
        # not to catch) and raises, so the caller's retry reconnects. Off the
        # main thread SIGALRM is illegal, so we rely on the socket timeout.
        on_main_thread = threading.current_thread() is threading.main_thread()
        if not (on_main_thread and hasattr(signal, "SIGALRM")):
            data = _do_request()
        else:
            def _timeout(_signum: int, _frame: Any) -> None:
                raise TimeoutError(
                    f"OpenRouter request exceeded hard timeout of {_HARD_TIMEOUT}s "
                    "(likely a half-open socket after a network drop)"
                )

            previous = signal.signal(signal.SIGALRM, _timeout)
            signal.setitimer(signal.ITIMER_REAL, _HARD_TIMEOUT)
            try:
                data = _do_request()
            finally:
                signal.setitimer(signal.ITIMER_REAL, 0)
                signal.signal(signal.SIGALRM, previous)

        choices = data.get("choices")
        if not choices:
            raise ValueError(f"OpenRouter returned no choices: {json.dumps(data)}")

        return choices[0]["message"]["content"]


# Module-level singleton, lazily initialised on first call.
_client: Optional[OpenRouterClient] = None


def _get_client() -> OpenRouterClient:
    global _client
    if _client is None:
        _client = OpenRouterClient()
    return _client


def call_openrouter(
    messages: List[Dict[str, str]],
    **kwargs: Any,
) -> str:
    """Convenience function — calls OpenRouter with the default client.

    Accepts the same keyword arguments as ``OpenRouterClient.chat()``.
    """
    return _get_client().chat(messages, **kwargs)
