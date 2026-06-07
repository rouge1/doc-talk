"""Reranker unit tests: the reorder math, the graceful fallback, and availability.

These avoid loading the cross-encoder (no downloads) — the model path is monkeypatched. The
end-to-end rerank quality is checked in the live verification, not here.
"""

from __future__ import annotations

import pytest

from doctalk.query.retriever import Hit, _order_by_rerank, _rerank_and_order, _sigmoid


def _hit(chunk_id: int, text: str, score: float) -> Hit:
    return Hit(chunk_id=chunk_id, file="f.pdf", chapter=None, page=chunk_id, text=text, score=score)


def test_sigmoid_is_bounded_and_monotonic():
    assert _sigmoid(0.0) == pytest.approx(0.5)
    assert _sigmoid(-2.0) < _sigmoid(0.0) < _sigmoid(2.0)  # monotonic
    assert 0.0 < _sigmoid(-3.0) and _sigmoid(3.0) < 1.0    # strictly inside (0, 1)
    # overflow-safe at extremes (saturates without raising)
    assert _sigmoid(1000.0) == pytest.approx(1.0)
    assert _sigmoid(-1000.0) == pytest.approx(0.0)


def test_order_by_rerank_sorts_by_score_and_truncates():
    # ANN order (by cosine) is a, b, c — but the cross-encoder prefers c, then a, then b.
    hits = [_hit(1, "a", 0.9), _hit(2, "b", 0.8), _hit(3, "c", 0.7)]
    ordered = _order_by_rerank(hits, scores=[1.0, -2.0, 5.0], k=2)
    assert [h.chunk_id for h in ordered] == [3, 1]  # c (5.0) then a (1.0); b dropped
    assert all(0.0 <= h.rerank_score <= 1.0 for h in ordered)
    assert ordered[0].rerank_score > ordered[1].rerank_score


def test_rerank_and_order_reorders_on_success(monkeypatch):
    from doctalk.models import rerank as rr

    # Cross-encoder flips the ANN order: last candidate is actually the most relevant.
    monkeypatch.setattr(rr, "rerank", lambda q, passages: [float(i) for i in range(len(passages))])
    hits = [_hit(1, "a", 0.9), _hit(2, "b", 0.8), _hit(3, "c", 0.7)]
    ordered = _rerank_and_order("q", hits, k=3)
    assert [h.chunk_id for h in ordered] == [3, 2, 1]


def test_rerank_and_order_falls_back_to_ann_order_on_failure(monkeypatch):
    from doctalk.models import rerank as rr

    def boom(q, passages):
        raise RuntimeError("model unavailable")

    monkeypatch.setattr(rr, "rerank", boom)
    hits = [_hit(1, "a", 0.9), _hit(2, "b", 0.8), _hit(3, "c", 0.7)]
    ordered = _rerank_and_order("q", hits, k=2)
    assert [h.chunk_id for h in ordered] == [1, 2]  # untouched ANN order, truncated to k
    assert all(h.rerank_score is None for h in ordered)


def test_rerank_available_returns_bool():
    from doctalk.models.rerank import rerank_available

    assert isinstance(rerank_available(), bool)
