"""A tiny, safe Markdown renderer for the wiki browser.

Renders only the subset the synthesis layer emits (headings, lists, a blockquote meta line, rules,
``[[wikilinks]]``, bold, and italic citations) — not a general Markdown engine. Everything is
HTML-escaped *before* markup is applied, and wikilink targets are constrained to slug characters, so
neither authored prose nor a crafted page name can inject markup. Wikilinks resolve to ``/wiki/page``.
"""

from __future__ import annotations

import re

from markupsafe import Markup, escape

_WIKILINK = re.compile(r"\[\[([a-z0-9-]+)(?:\|([^\]]*))?\]\]")
_BOLD = re.compile(r"\*\*([^*]+)\*\*")
_CITE = re.compile(r"_\(([^)]*)\)_")          # _(source: …)_ provenance
_ITALIC = re.compile(r"\*([^*\n]+)\*")        # single-asterisk italic (filenames keep underscores)


def _inline(escaped: str) -> str:
    """Apply inline markup to already-HTML-escaped text."""
    escaped = _WIKILINK.sub(
        lambda m: f'<a class="wikilink" href="/wiki/page/{m.group(1)}">{m.group(2) or m.group(1)}</a>',
        escaped,
    )
    escaped = _BOLD.sub(r"<strong>\1</strong>", escaped)
    escaped = _CITE.sub(r'<em class="cite">(\1)</em>', escaped)
    escaped = _ITALIC.sub(r"<em>\1</em>", escaped)
    return escaped


def _cell(text: str) -> str:
    return _inline(str(escape(text)))


def render(raw: str) -> Markup:
    """Render the wiki Markdown subset to safe HTML."""
    out: list[str] = []
    in_list = False

    def close_list() -> None:
        nonlocal in_list
        if in_list:
            out.append("</ul>")
            in_list = False

    for line in raw.splitlines():
        s = line.rstrip()
        if not s.strip():
            close_list()
            continue
        if s.startswith("# "):
            close_list()
            out.append(f'<h1 class="wiki-title">{_cell(s[2:])}</h1>')
        elif s.startswith("## "):
            close_list()
            out.append(f"<h2>{_cell(s[3:])}</h2>")
        elif s.startswith("> "):
            close_list()
            out.append(f'<p class="wiki-meta">{_cell(s[2:])}</p>')
        elif s.strip() == "---":
            close_list()
            out.append("<hr>")
        elif s.startswith("- "):
            if not in_list:
                out.append('<ul class="wiki-list">')
                in_list = True
            out.append(f"<li>{_cell(s[2:])}</li>")
        else:
            close_list()
            out.append(f"<p>{_cell(s)}</p>")
    close_list()
    return Markup("\n".join(out))
