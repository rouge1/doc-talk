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
