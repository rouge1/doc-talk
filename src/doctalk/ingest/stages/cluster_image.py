"""cluster_image — assign a freshly-embedded photo to a near-duplicate cluster.

Incremental single-link clustering against the whole image index: find the new image's CLIP
neighbours above ``cluster_sim_threshold``, then label it with the smallest file_id across itself
and those neighbours' existing clusters (the same component-min invariant the batch ``recluster``
command computes globally). If the new image *bridges* two previously-separate clusters, the
higher-labelled one is merged down into the lower — keeping the invariant intact.

Order-tolerant and idempotent: the new image is the newest (largest) id, so it adopts an existing
label rather than forcing a relabel of older clusters; re-running recomputes the same min and the
merges become no-ops. The authoritative, order-independent recompute is ``doctalk recluster``.
"""

from __future__ import annotations

from doctalk.config import get_settings
from doctalk.db import repo
from doctalk.ingest.dag import StageContext
from doctalk.vector import store


def run(ctx: StageContext) -> None:
    file_id = repo.get_file_id(ctx.session, ctx.content_hash)
    if file_id is None:  # pragma: no cover - defensive
        raise ValueError(f"cluster_image: no file row for {ctx.content_hash}")

    vector = ctx.scratch.get("image_vector") or store.get_image_vector(file_id)
    if vector is None:  # not embedded (e.g. embed_image was skipped) — nothing to cluster
        return

    s = get_settings()
    raw = store.search_images(vector, s.cluster_fetch_k)
    neighbours = [
        r["file_id"]
        for r in raw
        if r["file_id"] != file_id
        and (1.0 - float(r.get("_distance", 0.0))) >= s.cluster_sim_threshold
    ]

    # Each neighbour's component label (its stored cluster_id, or its own id if not yet clustered).
    clusters = repo.get_image_clusters(ctx.session, [file_id, *neighbours])
    labels = {clusters.get(nid) or nid for nid in neighbours}
    canonical = min({file_id, *labels})

    # Merge any bridged neighbour clusters down into the canonical label, then claim it.
    for label in labels:
        if label != canonical:
            repo.relabel_cluster(ctx.session, label, canonical)
    repo.set_image_cluster(ctx.session, file_id, canonical)
    ctx.scratch["cluster_id"] = canonical
