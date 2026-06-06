"""Vision-language description via Ollama (stdlib client).

Sends an image to a local multimodal model (default ``llama3.2-vision``) and returns a short
description. Same dependency-free HTTP approach as ``models.chat``. This is the GPU-heavy path;
PLAN reserves it for an offline batch behind a GPU lease — for Phase 1 we just call it per image.
"""

from __future__ import annotations

import base64
import json
import urllib.error
import urllib.request
from pathlib import Path

from doctalk.config import get_settings

DEFAULT_PROMPT = (
    "Describe this image in one or two sentences. Note any animals, objects, people, scene, "
    "and any visible text."
)


def describe_image(
    path: str | Path,
    *,
    prompt: str = DEFAULT_PROMPT,
    model: str | None = None,
    timeout: float = 300.0,
) -> str:
    settings = get_settings()
    image_b64 = base64.b64encode(Path(path).read_bytes()).decode("ascii")
    payload = {
        "model": model or settings.vlm_model,
        "messages": [{"role": "user", "content": prompt, "images": [image_b64]}],
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
            f"Ollama VLM request failed ({settings.ollama_host}): {exc}. Is the server running "
            f"and is model {payload['model']!r} pulled?"
        ) from exc
    return data["message"]["content"].strip()
