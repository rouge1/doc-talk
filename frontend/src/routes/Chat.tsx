import { useEffect, useState } from "react";
import { Link, Navigate, useSearchParams } from "react-router-dom";
import { api, sourcePath, type ChatAnswer } from "../api";
import { getCachedAnswer, getLastQuestion, setCachedAnswer, setLastQuestion } from "../answerStore";
import Answer from "../Answer";

const EMPTY: ChatAnswer = { query: "", answer: "", wiki_citations: [], citations: [] };

export default function Chat() {
  const [params, setParams] = useSearchParams();
  const q = params.get("q") ?? "";
  const [draft, setDraft] = useState(q);
  const [data, setData] = useState<ChatAnswer | null>(null);
  const [error, setError] = useState(false);

  // Cache-first: a cached answer (memory or localStorage) restores instantly when you come back from
  // a citation or a reload; only a cache miss runs the slow pipeline, and the result is then stored.
  useEffect(() => {
    setError(false);
    if (!q) {
      setData(EMPTY);
      return;
    }
    setLastQuestion(q); // remember it even on a cache hit, so the ASK tab can restore it
    const cached = getCachedAnswer(q);
    if (cached) {
      setData(cached);
      return;
    }
    setData(null);
    let alive = true;
    api
      .chat(q)
      .then((d) => {
        if (!alive) return;
        setData(d);
        setCachedAnswer(q, d);
      })
      .catch(() => alive && setError(true));
    return () => {
      alive = false;
    };
  }, [q]);

  // Keep the search box in sync with the question in the URL (e.g. after a restore redirect).
  useEffect(() => setDraft(q), [q]);

  const loading = !data && !error;

  // Landing on a bare /chat (clicking the ASK nav tab, or returning later) restores the last
  // question so switching tabs never loses the answer — redirect to its URL and the cache-first
  // effect above serves it instantly. Guard runs after all hooks, so hook order stays stable.
  if (!q) {
    const last = getLastQuestion();
    if (last) return <Navigate to={`/chat?q=${encodeURIComponent(last)}`} replace />;
  }

  const submit = (e: React.FormEvent) => {
    e.preventDefault();
    setParams(draft.trim() ? { q: draft.trim() } : {});
  };

  return (
    <div className="rise">
      <section className="hero compact">
        <div className="kicker">Wiki-first · answered from the synthesis layer</div>
        <h1 className="display">Ask the archive</h1>
      </section>

      <form className="searchbar" onSubmit={submit}>
        <input
          value={draft}
          onChange={(e) => setDraft(e.target.value)}
          placeholder="ask a question grounded in your corpus…"
          autoFocus
        />
        <button type="submit">Ask</button>
      </form>

      {q && loading && (
        <div className="thinking">
          <div className="q-echo mono">“{q}”</div>
          <div className="loading">Consulting the wiki and the local model… this can take ~a minute.</div>
        </div>
      )}
      {error && <div className="empty">The model didn't respond — is Ollama running?</div>}

      {data && q && !loading && (
        <article className="dispatch">
          <div className="dispatch-main">
            <div className="rule-head"><h2>Answer</h2></div>
            <Answer text={data.answer} citations={data.citations} />
          </div>

          {(data.wiki_citations.length > 0 || data.citations.length > 0) && (
            <aside className="dispatch-aside">
              {data.wiki_citations.length > 0 && (
                <section className="apparatus">
                  <div className="rule-head"><h2>From the wiki</h2></div>
                  <div className="cite-cards">
                    {data.wiki_citations.map((w, i) =>
                      w.stem ? (
                        <Link key={i} className="cite-card" to={`/wiki/entity/${w.stem}`}>
                          <span className="cc-name">{w.name}</span>
                          <span className="cc-type mono">{w.type}</span>
                        </Link>
                      ) : (
                        <span key={i} className="cite-card">
                          <span className="cc-name">{w.name}</span>
                          <span className="cc-type mono">{w.type}</span>
                        </span>
                      ),
                    )}
                  </div>
                </section>
              )}

              {data.citations.length > 0 && (
                <section className="apparatus">
                  <div className="rule-head"><h2>Sources</h2></div>
                  <ol className="sources">
                    {data.citations.map((c) => (
                      <li key={c.n}>
                        <Link to={sourcePath(c)}>
                          {c.file} · {c.chapter ?? "—"} · p.{c.page}
                        </Link>
                      </li>
                    ))}
                  </ol>
                </section>
              )}
            </aside>
          )}
        </article>
      )}
    </div>
  );
}
