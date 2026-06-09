"""Minimal Ollama chat client (stdlib only — no extra dependency).

Talks to the local Ollama server's ``/api/chat`` endpoint. Keeping this dependency-free avoids
pinning an SDK; if we later need streaming or tool-calls we can swap in the official client.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request

from doctalk.config import get_settings


def chat(
    messages: list[dict[str, str]],
    *,
    model: str | None = None,
    timeout: float = 180.0,
    format: str | dict | None = None,
    options: dict | None = None,
    think: bool | None = None,
) -> str:
    """Send a chat completion to Ollama and return the assistant's text.

    ``messages`` is the standard ``[{"role": ..., "content": ...}]`` list. ``format`` forwards
    Ollama's structured-output control — ``"json"`` (JSON mode) or a JSON-schema dict — used by the
    synthesis extractor to force machine-parseable output. ``options`` passes through sampling knobs
    (e.g. ``{"temperature": 0}``) and overrides the defaults below.

    ``think`` toggles a thinking model's reasoning pass (default: ``chat_think``). It matters: a
    reasoning model left at its default puts its whole answer in a separate ``thinking`` field, hits
    the token limit mid-reasoning, and returns an EMPTY ``content``. We disable it so the model
    answers directly, and set ``num_ctx``/``num_predict`` so a large wiki prompt + answer both fit
    (Ollama's 4 K default silently truncates). Raises a clear error if the server is unreachable."""
    settings = get_settings()
    payload: dict = {
        "model": model or settings.chat_model,
        "messages": messages,
        "stream": False,
        "think": settings.chat_think if think is None else think,
    }
    if format is not None:
        payload["format"] = format
    # Caller options win; otherwise give the prompt + answer real room (see chat_num_ctx rationale).
    payload["options"] = {
        "num_ctx": settings.chat_num_ctx,
        "num_predict": settings.chat_num_predict,
        **(options or {}),
    }
    request = urllib.request.Request(
        f"{settings.ollama_host.rstrip('/')}/api/chat",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            data = json.loads(response.read())
    except (urllib.error.URLError, TimeoutError) as exc:  # pragma: no cover - network/env dependent
        # socket read timeout surfaces as a bare TimeoutError (NOT a URLError subclass) — catch both
        # so callers get one clear, catchable failure instead of an opaque "timed out".
        raise RuntimeError(
            f"Ollama request failed ({settings.ollama_host}): {exc}. Is the server running, "
            f"responsive, and is model {payload['model']!r} pulled?"
        ) from exc
    return data["message"]["content"]
