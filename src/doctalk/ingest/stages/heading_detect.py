"""heading_detect — reconstruct an outline for PDFs that have no embedded TOC.

PyMuPDF exposes per-span typography (font size, weight) via ``get_text("dict")``. Headings are
larger and/or bold, short, and often section-numbered ("4.2.1 Encryption"); body text is the most
common size. Running headers/footers repeat across many pages and are suppressed. The output is
the same ``[level, title, page]`` shape as ``Document.get_toc()`` so the existing chapter-tree
builder consumes it unchanged — only tagged ``source="heading_detect"``.

This is heuristic and noisier than an embedded outline; the ``source`` column lets consumers flag
it as inferred. Numbering is the strongest signal, so technical/structured documents fare best.
"""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass

_NUMBERED = re.compile(r"^(\d+(?:\.\d+)*)\.?\s+\S")
_BOLD_FLAG = 1 << 4  # PyMuPDF span flag bit for bold
_MAX_TITLE_CHARS = 160
_MAX_TITLE_WORDS = 20
_MAX_LEVELS = 6


@dataclass
class _Line:
    text: str
    size: float  # rounded dominant font size on the line
    bold: bool
    page: int  # 1-based


def _is_bold(span: dict) -> bool:
    if span.get("flags", 0) & _BOLD_FLAG:
        return True
    name = span.get("font", "").lower()
    return any(tag in name for tag in ("bold", "black", "heavy", "semibold"))


def _collect_lines(doc) -> list[_Line]:
    lines: list[_Line] = []
    for page_index in range(doc.page_count):
        data = doc[page_index].get_text("dict")
        for block in data.get("blocks", []):
            if block.get("type") != 0:  # text blocks only
                continue
            for line in block.get("lines", []):
                spans = [s for s in line.get("spans", []) if s.get("text", "").strip()]
                if not spans:
                    continue
                text = "".join(s["text"] for s in spans).strip()
                if not text:
                    continue
                lines.append(
                    _Line(
                        text=text,
                        size=round(max(s["size"] for s in spans), 1),
                        bold=any(_is_bold(s) for s in spans),
                        page=page_index + 1,
                    )
                )
    return lines


def _body_size(lines: list[_Line]) -> float:
    """The character-weighted most common font size — i.e. the body text size."""
    weight: Counter[float] = Counter()
    for line in lines:
        weight[line.size] += len(line.text)
    return weight.most_common(1)[0][0] if weight else 0.0


def _repeated_texts(lines: list[_Line], page_count: int) -> set[str]:
    """Running headers/footers: short lines that recur across many pages."""
    pages_with: Counter[str] = Counter()
    seen: set[tuple[int, str]] = set()
    for line in lines:
        key = (line.page, line.text)
        if key in seen:
            continue
        seen.add(key)
        pages_with[line.text] += 1
    threshold = max(3, int(page_count * 0.10))
    return {t for t, n in pages_with.items() if n >= threshold and len(t) <= 120}


def _level_for(line: _Line, body_size: float, size_rank: dict[float, int]) -> int | None:
    """The heading level for a line, or None if it is not a heading.

    Numbered headings win (depth from the number, e.g. "1.2.3" -> 3); otherwise a clearly larger
    font maps to a level by size rank. Long/paragraph-like lines are rejected.
    """
    if len(line.text) > _MAX_TITLE_CHARS or len(line.text.split()) > _MAX_TITLE_WORDS:
        return None
    bigger = line.size >= body_size + 1.0
    numbered = _NUMBERED.match(line.text)
    if numbered and (line.bold or bigger):
        return min(numbered.group(1).count(".") + 1, _MAX_LEVELS)
    if bigger and (line.bold or line.size in size_rank):
        return size_rank.get(line.size, 1)
    return None


def detect_headings(doc) -> list[list]:
    """Return ``[[level, title, page], ...]`` in document order (empty if no text layer)."""
    lines = _collect_lines(doc)
    if not lines:
        return []  # no text layer (likely scanned) -> nothing to detect; OCR is a later stage
    body_size = _body_size(lines)
    repeated = _repeated_texts(lines, doc.page_count)

    big_sizes = sorted({ln.size for ln in lines if ln.size >= body_size + 1.0}, reverse=True)
    size_rank = {size: rank + 1 for rank, size in enumerate(big_sizes[:_MAX_LEVELS])}

    entries: list[list] = []
    for line in lines:
        if line.text in repeated:
            continue
        level = _level_for(line, body_size, size_rank)
        if level is not None:
            entries.append([level, line.text, line.page])
    return entries
