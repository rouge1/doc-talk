"""blake3 content hashing — the idempotency key for the whole pipeline.

``hash_file`` produces the ``content_hash`` that uniquely identifies a source (re-dropping the
same bytes is a no-op). ``job_input_hash`` derives the per-stage ledger key
``blake3(content_hash + stage + model_version + params)`` so a model/param upgrade re-runs only
the affected stage + downstream, while an identical re-run is skipped.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import blake3

_CHUNK = 1 << 20  # 1 MiB — stream large PDFs without loading them whole


def hash_file(path: str | Path) -> str:
    """blake3 hex digest of a file's bytes, streamed in 1 MiB chunks."""
    h = blake3.blake3()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(_CHUNK), b""):
            h.update(chunk)
    return h.hexdigest()


def hash_bytes(data: bytes) -> str:
    """blake3 hex digest of an in-memory buffer."""
    return blake3.blake3(data).hexdigest()


def job_input_hash(
    content_hash: str,
    stage: str,
    model_version: str = "",
    params: dict[str, Any] | None = None,
) -> str:
    """Deterministic ledger key for one (source, stage, model, params) combination.

    Params are serialized with sorted keys so logically-identical inputs always hash the same,
    regardless of dict ordering.
    """
    payload = json.dumps(
        {
            "content_hash": content_hash,
            "stage": stage,
            "model_version": model_version,
            "params": params or {},
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return blake3.blake3(payload.encode("utf-8")).hexdigest()
