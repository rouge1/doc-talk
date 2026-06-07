"""Image near-duplicate clustering: the pure component logic and the incremental stage.

The pure ``cluster_components`` is exercised on hand-built vectors (real cosine). The stage's ANN
search and stored vector are monkeypatched, so no CLIP model loads and neighbours are deterministic
— the test covers the linking *logic* (adopt / new / bridge-merge / idempotency), not retrieval.
"""

from __future__ import annotations

from doctalk.cluster.grouping import cluster_components, cosine
from doctalk.db import repo
from doctalk.db.session import session_scope
from doctalk.ingest.dag import StageContext
from doctalk.ingest.stages import cluster_image


# --- pure component logic --------------------------------------------------


def test_cosine_basics():
    assert cosine([1.0, 0.0], [1.0, 0.0]) == 1.0
    assert abs(cosine([1.0, 0.0], [0.0, 1.0])) < 1e-9
    assert cosine([1.0, 1.0], [0.0, 0.0]) == 0.0  # zero vector -> 0, not NaN


def test_components_group_near_duplicates_and_label_is_min():
    # 10 and 20 are near-identical; 30 is orthogonal -> two clusters, labelled by the min id.
    vectors = {
        10: [1.0, 0.0],
        20: [0.99, 0.01],
        30: [0.0, 1.0],
    }
    labels = cluster_components(vectors, threshold=0.92)
    assert labels[10] == labels[20] == 10   # grouped under the smaller id
    assert labels[30] == 30                  # singleton labels itself
    assert labels[10] != labels[30]


def test_components_single_link_chaining_merges_transitively():
    # a~b and b~c but a≁c directly; single-link still unites all three under the min id.
    vectors = {
        1: [1.0, 0.0, 0.0],
        2: [0.96, 0.28, 0.0],   # close to 1
        3: [0.80, 0.60, 0.0],   # close to 2, further from 1
    }
    labels = cluster_components(vectors, threshold=0.90)
    assert labels[1] == labels[2] == labels[3] == 1


def test_components_is_order_independent_and_idempotent():
    vectors = {5: [1.0, 0.0], 7: [0.999, 0.001], 9: [0.0, 1.0]}
    a = cluster_components(vectors, 0.92)
    b = cluster_components(dict(reversed(list(vectors.items()))), 0.92)
    assert a == b == {5: 5, 7: 5, 9: 9}


# --- the incremental stage -------------------------------------------------


def _image(s, content_hash, cluster_id=None):
    repo.upsert_file(
        s, content_hash=content_hash, path=f"/{content_hash}", filename=f"{content_hash}.png",
        format="png", mime="image/png", byte_size=1,
    )
    s.flush()
    fid = repo.get_file_id(s, content_hash)
    repo.upsert_image(s, fid)
    if cluster_id is not None:
        repo.set_image_cluster(s, fid, cluster_id)
    return fid


def _run(content_hash, hits, monkeypatch):
    """Run cluster_image for one file with a fixed set of synthetic neighbour hits."""
    monkeypatch.setattr(cluster_image.store, "search_images", lambda v, k, where=None: hits)
    with session_scope() as s:
        cluster_image.run(StageContext(content_hash, None, s, {"image_vector": [0.0]}))


def test_new_image_adopts_existing_cluster(db, monkeypatch):
    with session_scope() as s:
        a = _image(s, "a" * 64)          # gets cluster a on its own run below
        b = _image(s, "b" * 64)
    # A has no neighbours -> singleton cluster = its own id.
    _run("a" * 64, [], monkeypatch)
    # B is near A -> adopts A's (smaller) label.
    _run("b" * 64, [{"file_id": a, "_distance": 0.05}], monkeypatch)
    with session_scope() as s:
        clusters = repo.get_image_clusters(s, [a, b])
    assert clusters[a] == a
    assert clusters[b] == a


def test_distant_image_starts_its_own_cluster(db, monkeypatch):
    with session_scope() as s:
        a = _image(s, "a" * 64, cluster_id=None)
        b = _image(s, "b" * 64)
    # B's only neighbour is below threshold (sim 0.5) -> new singleton labelled by own id.
    _run("b" * 64, [{"file_id": a, "_distance": 0.5}], monkeypatch)
    with session_scope() as s:
        assert repo.get_image_clusters(s, [b])[b] == b


def test_bridging_image_merges_two_clusters(db, monkeypatch):
    with session_scope() as s:
        a = _image(s, "a" * 64, cluster_id=None)
        c = _image(s, "c" * 64, cluster_id=None)
        d = _image(s, "d" * 64)
        repo.set_image_cluster(s, a, a)   # two separate singleton clusters
        repo.set_image_cluster(s, c, c)
    # D is near both A and C -> all collapse to the min label (a), C relabelled.
    _run("d" * 64, [{"file_id": a, "_distance": 0.05}, {"file_id": c, "_distance": 0.05}], monkeypatch)
    with session_scope() as s:
        clusters = repo.get_image_clusters(s, [a, c, d])
    assert clusters[a] == clusters[c] == clusters[d] == a


def test_rerun_is_idempotent(db, monkeypatch):
    with session_scope() as s:
        a = _image(s, "a" * 64, cluster_id=None)
        b = _image(s, "b" * 64)
        repo.set_image_cluster(s, a, a)
    hits = [{"file_id": a, "_distance": 0.05}]
    _run("b" * 64, hits, monkeypatch)
    _run("b" * 64, hits, monkeypatch)
    with session_scope() as s:
        assert repo.get_image_clusters(s, [a, b]) == {a: a, b: a}
