"""Minimal Ollama chat client (stdlib only — no extra dependency).

Talks to the local Ollama server's ``/api/chat`` endpoint. Keeping this dependency-free avoids
pinning an SDK; if we later need streaming or tool-calls we can swap in the official client.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request

from doctalk.config import get_settings


def chat(messages: list[dict[str, str]], *, model: str | None = None, timeout: float = 180.0) -> str:
    """Send a chat completion to Ollama and return the assistant's text.

    ``messages`` is the standard ``[{"role": ..., "content": ...}]`` list. Raises a clear error
    if the server is unreachable (e.g. Ollama not running)."""
    settings = get_settings()
    payload = {
        "model": model or settings.chat_model,
        "messages": messages,
        "stream": False,
    }
    request = urllib.request.Request(
        f"{settings.ollama_host.rstrip('/')}/api/chat",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            data = json.loads(response.read())
    except urllib.error.URLError as exc:  # pragma: no cover - network/env dependent
        raise RuntimeError(
            f"Ollama request failed ({settings.ollama_host}): {exc}. Is the server running "
            f"and is model {payload['model']!r} pulled?"
        ) from exc
    return data["message"]["content"]
