import { type ReactNode } from "react";
import { Link } from "react-router-dom";
import { sourcePath, type Citation } from "./api";

// A deliberately small GitHub-flavored-markdown renderer — no dependency, offline-safe. It covers
// exactly what the local answer model emits (headings, bold, italic, inline code, bullet/numbered
// lists, paragraphs) and, crucially, weaves the inline [n] citation markers into source links so a
// long structured answer reads like a set reference article rather than raw markup.

// Inline spans, longest-delimiter-first so **bold** is consumed before *italic*.
const INLINE = /(\*\*[^*]+\*\*|__[^_]+__|\*[^*\n]+\*|`[^`]+`|\[\d+\])/g;

// The local model's citation style drifts wildly run-to-run: [1], [[1]], [1][2], and compound tags
// that bury a number in label noise like [2; "Advertising procedure (concept)"] or
// [[SYNTHESIZED KNOWLEDGE: …]]. Rather than chase each form, collapse EVERY bracket group to just the
// citation numbers it contains ([2; "…"] -> [2], [,3; Excerpt from ,4] -> [3][4], a label-only tag
// -> dropped). The inline pass then turns the clean [n] into source links.
function normalize(text: string): string {
  return text
    // The model sometimes echoes a source's raw provenance inline — "(source: Core_v6.0.pdf p.95)"
    // or "[10](file: …)" — instead of a clean [n]. Strip those parentheticals; the Sources rail is
    // where provenance belongs, not mid-sentence.
    .replace(/\((?:file|source):[^)]*\)/gi, "")
    .replace(/\[+([^[\]]*)\]+/g, (_m, inner: string) => {
      const nums = inner.match(/\d+/g);
      return nums ? nums.map((n) => `[${n}]`).join("") : "";
    })
    // Merge a run of citations separated only by spaces/commas ([4], [1] -> [4][1]) so the comma
    // between them comes solely from the renderer's adjacency rule — never doubled with literal text.
    .replace(/\[\d+\](?:\s*,?\s*\[\d+\])+/g, (m) => {
      const nums = m.match(/\d+/g) as string[];
      return nums.map((n) => `[${n}]`).join("");
    })
    .replace(/[ \t]+([;,.)])/g, "$1") // tidy whitespace left before punctuation
    .replace(/\(\s*\)/g, "") // drop parens emptied by the removals
    .replace(/[ \t]{2,}/g, " ");
}

function inline(text: string, citations: Citation[], kp: string): ReactNode[] {
  const out: ReactNode[] = [];
  let last = 0;
  let i = 0;
  let m: RegExpExecArray | null;
  INLINE.lastIndex = 0;
  while ((m = INLINE.exec(text)) !== null) {
    if (m.index > last) out.push(text.slice(last, m.index));
    const tok = m[0];
    const key = `${kp}-${i++}`;
    if (tok.startsWith("**") || tok.startsWith("__")) {
      out.push(<strong key={key}>{tok.slice(2, -2)}</strong>);
    } else if (tok.startsWith("`")) {
      out.push(<code key={key} className="md-code">{tok.slice(1, -1)}</code>);
    } else if (tok.startsWith("*")) {
      out.push(<em key={key}>{tok.slice(1, -1)}</em>);
    } else {
      const n = Number(tok.slice(1, -1));
      const c = citations.find((x) => x.n === n);
      // Only [n] that resolves to a real source becomes a mark. The model is given excerpts numbered
      // [1..N], so anything out of range is an artifact — a hallucinated index, or a page number it
      // mistook for a citation (e.g. [292]). Drop it rather than leak raw brackets into the prose.
      if (c) {
        out.push(
          <Link key={key} className="cite-mark" to={sourcePath(c)} title={`${c.file} · p.${c.page}`}>
            {n}
          </Link>,
        );
      }
    }
    last = m.index + tok.length;
  }
  if (last < text.length) out.push(text.slice(last));
  return out;
}

export default function Answer({ text, citations }: { text: string; citations: Citation[] }) {
  const lines = normalize(text.replace(/\r/g, "")).split("\n");
  const blocks: ReactNode[] = [];
  let para: string[] = [];
  let quote: string[] = [];
  let list: { items: string[]; ordered: boolean } | null = null;
  let k = 0;

  const flushPara = () => {
    if (para.length) {
      blocks.push(<p key={`b${k}`}>{inline(para.join(" "), citations, `p${k}`)}</p>);
      k++;
      para = [];
    }
  };
  const flushQuote = () => {
    if (quote.length) {
      // The standfirst/dek — the formatter's one-line direct answer, set as the lead.
      blocks.push(
        <blockquote key={`b${k}`} className="dek">{inline(quote.join(" "), citations, `q${k}`)}</blockquote>,
      );
      k++;
      quote = [];
    }
  };
  const flushList = () => {
    if (list) {
      const { items, ordered } = list;
      const lis = items.map((it, j) => <li key={j}>{inline(it, citations, `l${k}-${j}`)}</li>);
      blocks.push(
        ordered ? (
          <ol key={`b${k}`} className="md-list">{lis}</ol>
        ) : (
          <ul key={`b${k}`} className="md-list">{lis}</ul>
        ),
      );
      k++;
      list = null;
    }
  };

  for (const raw of lines) {
    const line = raw.trimEnd();
    const h = line.match(/^(#{1,6})\s+(.*)$/);
    const q = line.match(/^>\s?(.*)$/);
    const b = line.match(/^\s*[*-]\s+(.*)$/);
    const o = line.match(/^\s*\d+\.\s+(.*)$/);
    if (q) {
      flushPara();
      flushList();
      quote.push(q[1]);
    } else if (h) {
      flushPara();
      flushQuote();
      flushList();
      const lvl = Math.min(h[1].length, 3);
      blocks.push(
        <p key={`b${k}`} className={`md-head md-h${lvl}`}>{inline(h[2], citations, `h${k}`)}</p>,
      );
      k++;
    } else if (b) {
      flushPara();
      flushQuote();
      if (!list || list.ordered) {
        flushList();
        list = { items: [], ordered: false };
      }
      list.items.push(b[1]);
    } else if (o) {
      flushPara();
      flushQuote();
      if (!list || !list.ordered) {
        flushList();
        list = { items: [], ordered: true };
      }
      list.items.push(o[1]);
    } else if (!line.trim()) {
      flushPara();
      flushQuote();
      flushList();
    } else {
      flushQuote();
      flushList();
      para.push(line.trim());
    }
  }
  flushPara();
  flushQuote();
  flushList();

  return <div className="answer-prose">{blocks}</div>;
}
