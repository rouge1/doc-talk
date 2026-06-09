"""Shared chunk-noise heuristic.

A table-of-contents / index chunk is mostly dotted-leader lines pointing at page numbers
("Channel Sounding ............ 339"). Such chunks match queries by coincidence (they contain the
words) but carry no readable content, so they crowd out real passages in both retrieval and
synthesis extraction. Both the search retriever and ``synth_entities`` filter on this, so the rule
lives here once.
"""

from __future__ import annotations

import re

# A line that trails off in >=4 dots, optionally to a page number.
_LEADER_LINE = re.compile(r"\.{4,}\s*\d*\s*$")

# Page furniture: running headers/footers that some PDF chunks consist entirely of.
_BOILERPLATE_LINE = re.compile(
    r"(?i)^\s*("
    r"version date:\s*[\d-]+"
    r"|bluetooth sig proprietary"
    r"|page\s+\d+"
    r"|bluetooth core specification\b.*"
    r")\s*$"
)


def is_toc_noise(text: str) -> bool:
    """True for table-of-contents / index chunks (a third or more dotted-leader lines)."""
    lines = [ln for ln in text.splitlines() if ln.strip()]
    if len(lines) < 4:
        return False
    leaders = sum(1 for ln in lines if _LEADER_LINE.search(ln))
    return leaders / len(lines) >= 0.3


def is_boilerplate(text: str) -> bool:
    """True for chunks made up *entirely* of page furniture (e.g. "Version Date: …", a page-header
    line, "Bluetooth SIG Proprietary"). Stray sub-3-char fragment lines (a word split across a page
    boundary, like a lone "y") are ignored; every remaining line must be furniture, so real prose
    that merely ends with a footer line is never dropped."""
    lines = [ln.strip() for ln in text.splitlines() if len(ln.strip()) >= 3]
    return bool(lines) and len(lines) <= 4 and all(_BOILERPLATE_LINE.match(ln) for ln in lines)


def is_noise_chunk(text: str) -> bool:
    """A chunk that should never be a retrieval/synthesis result: table-of-contents or page furniture."""
    return is_toc_noise(text) or is_boilerplate(text)
