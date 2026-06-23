import { useEffect, useState } from "react";
import { Link, Navigate, useSearchParams } from "react-router-dom";
import { api, sourcePath, type SearchMode, type SearchResult } from "../api";
import { getCached, getLastKey, setCached, setLastKey } from "../cache";

const EMPTY: SearchResult = { query: "", hits: [] };
const NS = "search";

// How each result was found — so a result with no highlighted words reads as "matched by meaning"
// rather than looking broken.
const ARM_LABEL: Record<string, string> = {
  keyword: "keyword",
  semantic: "semantic",
  both: "keyword + semantic",
};

// Stopwords kept in sync with the backend (retriever._STOPWORDS) so we don't light up function words.
const STOPWORDS = new Set(
  ("a an and are as at be but by for from has have in into is it its of on or that the their " +
    "this to was were will with").split(" "),
);
const escRe = (s: string) => s.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");

const SNIPPET = 280;
const LEAD = 48; // chars of context kept before the match when windowing

// The query needles that Simple actually matched: quoted phrases (exact) + content words (stopwords
// dropped, version/dot-aware so "6.0" stays whole). Lower-cased; phrases first so the longer match
// wins in the alternation.
function needlesOf(query: string): string[] {
  const phrases = [...query.matchAll(/"([^"]+)"/g)].map((m) => m[1].trim().toLowerCase()).filter(Boolean);
  const rest = query.replace(/"[^"]*"/g, " ").toLowerCase();
  const words = [...new Set(rest.match(/[a-z0-9]+(?:[.\-][a-z0-9]+)*/g) ?? [])].filter(
    (t) => t.length >= 2 && !STOPWORDS.has(t),
  );
  return [...phrases, ...words];
}

// Keyword-in-context window: center the preview on the first match (not always char 0) so a match
// deep inside a long chunk is actually visible, then truncate with ellipses.
function windowed(text: string, needles: string[]): string {
  const low = text.toLowerCase();
  let idx = -1;
  for (const n of needles) {
    const i = low.indexOf(n);
    if (i >= 0 && (idx < 0 || i < idx)) idx = i;
  }
  const start = idx > LEAD ? idx - LEAD : 0;
  let win = text.slice(start, start + SNIPPET);
  if (start > 0) win = "…" + win;
  if (start + SNIPPET < text.length) win = win + "…";
  return win;
}

// Render a snippet centered on the match, with the matched word(s)/phrase(s) highlighted (substring,
// case-insensitive — "channel" lights up inside "channels").
function highlightSnippet(text: string, query: string) {
  const needles = needlesOf(query);
  if (!needles.length) return text.slice(0, SNIPPET) + (text.length > SNIPPET ? "…" : "");
  const win = windowed(text, needles);
  const re = new RegExp(`(${needles.map(escRe).join("|")})`, "gi");
  return win.split(re).map((part, i) =>
    needles.includes(part.toLowerCase()) ? (
      <mark key={i} className="hl-term">{part}</mark>
    ) : (
      part
    ),
  );
}

export default function Search() {
  const [params, setParams] = useSearchParams();
  const q = params.get("q") ?? "";
  const mode: SearchMode = params.get("mode") === "simple" ? "simple" : "hybrid";
  const [draft, setDraft] = useState(q);
  const [data, setData] = useState<SearchResult | null>(null);
  const [error, setError] = useState(false);

  const cacheKey = `${mode}:${q}`;

  // Cache-first (keyed on mode + query): restore prior results instantly on return / reload / tab
  // switch; only a cache miss hits the index, and the result is then stored.
  useEffect(() => {
    setError(false);
    if (!q) {
      setData(EMPTY);
      return;
    }
    setLastKey(NS, `q=${encodeURIComponent(q)}&mode=${mode}`); // restore exact view from the SEARCH tab
    const cached = getCached<SearchResult>(NS, cacheKey);
    if (cached) {
      setData(cached);
      return;
    }
    setData(null);
    let alive = true;
    api
      .search(q, mode)
      .then((d) => {
        if (!alive) return;
        setData(d);
        setCached(NS, cacheKey, d);
      })
      .catch(() => alive && setError(true));
    return () => {
      alive = false;
    };
  }, [cacheKey, q, mode]);

  useEffect(() => setDraft(q), [q]); // keep the box in sync with the URL query

  const loading = !data && !error;

  // Landing on a bare /search (the SEARCH nav tab, or returning later) restores the last view so
  // switching tabs never loses results — redirect to its URL; the cache-first effect serves it.
  if (!q) {
    const last = getLastKey(NS);
    if (last) return <Navigate to={`/search?${last}`} replace />;
  }

  const run = (nextQ: string, nextMode: SearchMode) => {
    const next: Record<string, string> = {};
    if (nextQ.trim()) next.q = nextQ.trim();
    if (nextMode === "simple") next.mode = "simple"; // hybrid is the default; keep the URL clean
    setParams(next);
  };

  return (
    <div className="rise">
      <section className="hero compact">
        <div className="kicker">
          {mode === "simple" ? "Simple · keyword match" : "Hybrid · keyword + semantic, reranked"}
        </div>
        <h1 className="display">Search the stacks</h1>
      </section>

      <form
        className="searchbar"
        onSubmit={(e) => {
          e.preventDefault();
          run(draft, mode);
        }}
      >
        <div className="mode-toggle" role="radiogroup" aria-label="Search mode">
          <button
            type="button"
            className={mode === "simple" ? "on" : ""}
            aria-pressed={mode === "simple"}
            onClick={() => run(draft || q, "simple")}
          >
            Simple
          </button>
          <button
            type="button"
            className={mode === "hybrid" ? "on" : ""}
            aria-pressed={mode === "hybrid"}
            onClick={() => run(draft || q, "hybrid")}
          >
            Hybrid
          </button>
        </div>
        <input
          value={draft}
          onChange={(e) => setDraft(e.target.value)}
          placeholder={
            mode === "simple"
              ? "keyword(s) — exact words across every document…"
              : "meaning + keywords across every document…"
          }
          autoFocus
        />
        <button type="submit">Search</button>
      </form>

      {q && loading && <div className="loading">Searching the index…</div>}
      {error && <div className="empty">Search failed — is the server running?</div>}
      {data && q && !loading && data.hits.length === 0 && (
        <div className="empty">
          {mode === "simple" ? (
            <>
              No exact matches for “{q}”.{" "}
              {/* Simple is literal — a plural or paraphrase won't match. Point at the mode that does. */}
              <button type="button" className="link-btn" onClick={() => run(q, "hybrid")}>
                Try Hybrid for meaning →
              </button>
            </>
          ) : (
            <>Nothing matched “{q}”.</>
          )}
        </div>
      )}

      <div className="results">
        {data?.hits.map((h, i) =>
          h.kind === "image" ? (
            // A photo, matched by its VLM caption. The thumbnail signals it's an image; clicking
            // opens the Gallery's visual search for the same words (where this plate ranks high) —
            // the two surfaces are one corpus, not two programs.
            <Link key={i} className="result result-image rise" style={{ animationDelay: `${i * 30}ms` }}
                  to={`/gallery?q=${encodeURIComponent(q)}`}>
              <img className="result-thumb" src={h.image ?? `/api/image/${h.file_id}`}
                   alt={h.file} loading="lazy"
                   onError={(e) => { e.currentTarget.style.display = "none"; }} />
              <div className="result-body">
                <div className="result-head">
                  <span className="score mono tnum">{(h.rerank_score ?? h.score).toFixed(2)}</span>
                  <span className="loc mono">{h.file}</span>
                  {h.source && <span className={`arm mono arm-${h.source}`}>{ARM_LABEL[h.source]}</span>}
                </div>
                <p className="snippet">{highlightSnippet(h.text, q)}</p>
              </div>
            </Link>
          ) : (
            <Link key={i} className="result rise" style={{ animationDelay: `${i * 30}ms` }}
                  to={sourcePath(h, q)}>
              <div className="result-head">
                <span className="score mono tnum">
                  {(h.rerank_score ?? h.score).toFixed(2)}
                </span>
                <span className="loc mono">{h.file} · {h.chapter ?? "—"} · p.{h.page}</span>
                {h.source && <span className={`arm mono arm-${h.source}`}>{ARM_LABEL[h.source]}</span>}
              </div>
              <p className="snippet">
                {highlightSnippet(h.text, q)}
              </p>
            </Link>
          ),
        )}
      </div>
    </div>
  );
}
