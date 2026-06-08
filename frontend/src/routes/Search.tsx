import { useState } from "react";
import { Link, useSearchParams } from "react-router-dom";
import { api, sourcePath, type SearchResult } from "../api";
import { useFetch } from "../useFetch";

const EMPTY: SearchResult = { query: "", hits: [] };

export default function Search() {
  const [params, setParams] = useSearchParams();
  const q = params.get("q") ?? "";
  const [draft, setDraft] = useState(q);
  const { data, error, loading } = useFetch(
    () => (q ? api.search(q) : Promise.resolve(EMPTY)),
    `search:${q}`,
  );

  const submit = (e: React.FormEvent) => {
    e.preventDefault();
    setParams(draft.trim() ? { q: draft.trim() } : {});
  };

  return (
    <div className="rise">
      <section className="hero compact">
        <div className="kicker">Hybrid retrieval · ANN + cross-encoder</div>
        <h1 className="display">Search the stacks</h1>
      </section>

      <form className="searchbar" onSubmit={submit}>
        <input
          value={draft}
          onChange={(e) => setDraft(e.target.value)}
          placeholder="meaning + keywords across every document…"
          autoFocus
        />
        <button type="submit">Search</button>
      </form>

      {q && loading && <div className="loading">Searching the index…</div>}
      {error && <div className="empty">Search failed — is the server running?</div>}
      {data && q && !loading && data.hits.length === 0 && (
        <div className="empty">No passages matched “{q}”.</div>
      )}

      <div className="results">
        {data?.hits.map((h, i) => (
          <Link key={i} className="result rise" style={{ animationDelay: `${i * 30}ms` }}
                to={sourcePath(h)}>
            <div className="result-head">
              <span className="score mono tnum">
                {(h.rerank_score ?? h.score).toFixed(2)}
              </span>
              <span className="loc mono">{h.file} · {h.chapter ?? "—"} · p.{h.page}</span>
            </div>
            <p className="snippet">
              {h.text.slice(0, 280)}{h.text.length > 280 ? "…" : ""}
            </p>
          </Link>
        ))}
      </div>
    </div>
  );
}
