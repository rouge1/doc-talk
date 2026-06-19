"""Shared helpers for document-structure synthesis (``synth_topics`` + ``synth_source``).

Both chapter-rollup stages need to roll a file's mentions up to their top-level chapter, slugify
titles into stable page stems, and wikilink entity names into authored prose. These live here so
the two stages compute *identical* slugs: a source page's Contents list links to the very
``topics/<stem>--<slug>.md`` paths ``synth_topics`` writes, so they must agree on slugging or the
links dangle. Pure + DB-read helpers only (no model), so they stay testable without Ollama.
"""

from __future__ import annotations

import re

from doctalk.db import repo

_SLUG = re.compile(r"[^a-z0-9]+")


def slugify(text: str) -> str:
    return _SLUG.sub("-", text.lower()).strip("-")[:80].rstrip("-") or "topic"


def top_level_map(chapters: list) -> dict[int, "repo.Chapter"]:
    """chapter_id -> its level-1 ancestor (itself if already top-level)."""
    by_id = {c.id: c for c in chapters}
    out: dict[int, repo.Chapter] = {}
    for c in chapters:
        node = c
        while node.parent_id is not None and node.parent_id in by_id:
            node = by_id[node.parent_id]
        out[c.id] = node
    return out


def cluster_entities(session, file_id: int) -> dict[int, set[int]]:
    """top-level-chapter id -> entity ids this source mentions there (chunk-less mentions and
    chunks outside any chapter can't be located, so they don't join a cluster)."""
    top = top_level_map(repo.get_chapters(session, file_id))
    chapter_of = {c.id: c.chapter_id for c in repo.get_chunks(session, file_id)}
    clusters: dict[int, set[int]] = {}
    for m in repo.get_mentions_for_file(session, file_id):
        chapter_id = chapter_of.get(m.chunk_id) if m.chunk_id is not None else None
        if chapter_id is None or chapter_id not in top:
            continue
        clusters.setdefault(top[chapter_id].id, set()).add(m.entity_id)
    return clusters


_WIKILINK = re.compile(r"\[\[([^\]]*?)\]\]?")


def linkify(prose: str, refs: list[tuple[str, str]]) -> str:
    """Wikilink entity names in authored prose — deterministically, NOT trusting the model's
    brackets. A model asked to copy ``[[slug|Name]]`` inline often miscounts brackets when the
    name ends in a paren (``[[lmp|Link Manager (LMP)]`` — a broken link, seen live with qwen3.5)
    or drops the display half (``[[slug]]`` — the raw slug shows as text). So we first strip ALL
    existing wikilink markup back to plain display text, then re-add links ourselves: the first
    plain occurrence of each ref name, longest first (so 'unsalted butter' wins over 'butter');
    the lookbehind skips a name already inside a link we just added. Every emitted link targets a
    ref — a real entity page — so the prose never carries a dangling link."""
    slug_to_name = dict(refs)

    def _unwrap(m: "re.Match[str]") -> str:
        inner = m.group(1)
        if "|" in inner:
            return inner.split("|", 1)[1]          # [[slug|Display]] / [[slug|Display] -> Display
        return slug_to_name.get(inner, inner)      # [[slug]] -> Name when the slug is a known ref
    prose = _WIKILINK.sub(_unwrap, prose)

    for slug, name in sorted(refs, key=lambda r: len(r[1]), reverse=True):
        # trailing (?!\w) not \b: a name ending in punctuation ('… (LMP)') has no word boundary
        # before a following ','/'.' so \b would never match it — the very names the model botches.
        pattern = re.compile(rf"(?<![\[|\w]){re.escape(name)}(?!\w)")
        if f"[[{slug}|" not in prose:
            prose = pattern.sub(f"[[{slug}|{name}]]", prose, count=1)
    return prose
