"""Near-duplicate image clustering: connected components over CLIP-vision cosine.

Pure logic — no DB, no Lance. Two images are "the same thing" if their cosine >= ``threshold``;
a cluster is a connected component of that graph, **labelled by its smallest file_id** so the
labels are stable and order-independent (re-running yields byte-identical assignments, and the
canonical member is a real, durable image id rather than an opaque counter). Singletons cluster
with themselves (label == own id). O(n^2) — the corpus is gallery-scale photos, not millions; the
authoritative caller is ``doctalk recluster`` (a batch recompute, like ``rebuild-index``).
"""

from __future__ import annotations

import math


def cosine(a: list[float], b: list[float]) -> float:
    """Cosine similarity of two vectors (0.0 if either is a zero vector)."""
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(x * x for x in b))
    if na == 0.0 or nb == 0.0:
        return 0.0
    return dot / (na * nb)


def cluster_components(vectors: dict[int, list[float]], threshold: float) -> dict[int, int]:
    """Group images by single-link connected components of the >= ``threshold`` cosine graph.

    Returns ``{file_id: cluster_id}`` where ``cluster_id`` is the smallest file_id in the same
    component. Union-find always reparents the higher root under the lower, so ``find`` resolves
    to the component minimum.
    """
    ids = sorted(vectors)
    parent = {i: i for i in ids}

    def find(x: int) -> int:
        while parent[x] != x:
            parent[x] = parent[parent[x]]  # path compression
            x = parent[x]
        return x

    def union(a: int, b: int) -> None:
        ra, rb = find(a), find(b)
        if ra != rb:
            lo, hi = (ra, rb) if ra < rb else (rb, ra)
            parent[hi] = lo  # smaller id wins -> component min is the label

    for i in range(len(ids)):
        vi = vectors[ids[i]]
        for j in range(i + 1, len(ids)):
            if cosine(vi, vectors[ids[j]]) >= threshold:
                union(ids[i], ids[j])

    return {i: find(i) for i in ids}
