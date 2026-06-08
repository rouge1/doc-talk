"""The wiki Markdown subset renderer: structure, wikilinks, and HTML-injection safety."""

from __future__ import annotations

from doctalk.api.wikimd import render


def test_renders_headings_meta_lists_and_rules():
    html = str(render("# Cake\n\n> **product** · 2 source(s)\n\n## Claims\n\n- bake it\n- frost it\n\n---\n"))
    assert '<h1 class="wiki-title">Cake</h1>' in html
    assert '<p class="wiki-meta"><strong>product</strong>' in html
    assert "<h2>Claims</h2>" in html
    assert html.count("<li>") == 2 and "<ul" in html and "</ul>" in html
    assert "<hr>" in html


def test_wikilinks_resolve_to_wiki_routes_with_alias():
    html = str(render("See [[link-manager|Link Manager]] and [[oven]]."))
    assert '<a class="wikilink" href="/wiki/page/link-manager">Link Manager</a>' in html
    assert '<a class="wikilink" href="/wiki/page/oven">oven</a>' in html


def test_citation_italic_preserves_filename_underscores():
    html = str(render("- 2 cups flour _(source: classic_vanilla_cake_recipe.docx p.6)_"))
    # the citation italicizes, but the underscores inside the filename are NOT treated as italic
    assert 'class="cite">(source: classic_vanilla_cake_recipe.docx p.6)</em>' in html


def test_escapes_html_and_blocks_injection():
    html = str(render("# <script>alert(1)</script>\n\n- a & b < c"))
    assert "<script>" not in html and "&lt;script&gt;" in html
    assert "&amp; b &lt; c" in html


def test_wikilink_target_is_slug_constrained():
    # a non-slug target (path traversal attempt) is left as inert text, not a link
    html = str(render("[[../../etc/passwd|pwn]]"))
    assert "<a" not in html and "/wiki/page/" not in html
