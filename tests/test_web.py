"""Smoke tests for the Phase 1 web UI: pages render and routing works against an empty DB.

These avoid query params, so no embedding/LLM models are loaded — the data-backed query paths
(search/chat/find) are covered by their own slices' verification.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from doctalk.api.app import app


@pytest.fixture
def client(db):
    return TestClient(app)


def test_index_renders(client):
    r = client.get("/")
    assert r.status_code == 200
    assert "doctalk" in r.text and "Documents" in r.text


def test_static_pages_render(client):
    for path in ("/search", "/chat", "/gallery"):
        r = client.get(path)
        assert r.status_code == 200
        assert "doctalk" in r.text


def test_missing_doc_is_404(client):
    assert client.get("/doc/nope").status_code == 404


def test_gallery_empty_lists_nothing(client):
    r = client.get("/gallery")
    assert r.status_code == 200
    assert "No images match" in r.text


def test_gallery_blank_form_params_dont_422(client):
    """The gallery form submits empty fields as `fmt=&min_kb=`; blanks must be treated as
    "no filter", not a parse error (regression for the float_parsing 422)."""
    for qs in ("fmt=&min_kb=", "q=cat&fmt=&min_kb=", "q=cat&fmt=png&min_kb=100", "min_kb=notanumber"):
        r = client.get(f"/gallery?{qs}")
        assert r.status_code == 200, f"gallery?{qs} -> {r.status_code}: {r.text[:200]}"


def test_missing_figure_is_404(client):
    assert client.get("/figure/999999").status_code == 404


def test_missing_image_is_404(client):
    assert client.get("/image/999999").status_code == 404
