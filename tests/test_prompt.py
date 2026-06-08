"""Pure unit test for RAG prompt assembly (no models, no network, no DB)."""

from __future__ import annotations

from types import SimpleNamespace

from doctalk.query.prompt import build_messages, format_citations


def _hit(file, chapter, page, text, content_hash=None, chapter_id=None):
    return SimpleNamespace(
        file=file, chapter=chapter, page=page, text=text,
        content_hash=content_hash, chapter_id=chapter_id,
    )


def test_build_messages_grounds_and_numbers_sources():
    hits = [
        _hit("Core_v6.0.pdf", "7 Bit stream processing", 537, "E0 is the stream cipher…"),
        _hit("Core_v6.0.pdf", None, 28, "table of contents entry"),
    ]
    messages = build_messages("What is E0 encryption?", hits)

    assert [m["role"] for m in messages] == ["system", "user"]
    assert "only from the provided context" in messages[0]["content"].lower()
    user = messages[1]["content"]
    assert "[1]" in user and "[2]" in user
    assert "p.537" in user and "7 Bit stream processing" in user
    assert "n/a" in user  # chapterless hit renders gracefully
    assert "What is E0 encryption?" in user


def test_format_citations_numbers_from_one():
    hits = [_hit("a.pdf", "Ch 1", 5, "x"), _hit("a.pdf", "Ch 2", 9, "y")]
    cites = format_citations(hits)
    assert cites == [
        {"n": 1, "file": "a.pdf", "chapter": "Ch 1", "page": 5,
         "content_hash": None, "chapter_id": None, "chunk_id": None},
        {"n": 2, "file": "a.pdf", "chapter": "Ch 2", "page": 9,
         "content_hash": None, "chapter_id": None, "chunk_id": None},
    ]


def test_format_citations_carries_link_targets():
    """When a hit knows its source doc + chapter, the citation exposes them for a deep link."""
    cites = format_citations([_hit("a.pdf", "Ch 1", 5, "x", content_hash="abc123", chapter_id=84)])
    assert cites[0]["content_hash"] == "abc123" and cites[0]["chapter_id"] == 84


def test_format_citations_tolerates_link_less_hits():
    """Duck-typed: an object without content_hash/chapter_id still formats (targets are None)."""
    bare = SimpleNamespace(file="a.pdf", chapter="Ch 1", page=5, text="x")
    cites = format_citations([bare])
    assert cites[0]["content_hash"] is None and cites[0]["chapter_id"] is None
