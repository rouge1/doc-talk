import { useState } from "react";
import { Link, useSearchParams } from "react-router-dom";
import { api, sourcePath, type ChatAnswer, type Citation } from "../api";
import { useFetch } from "../useFetch";

const EMPTY: ChatAnswer = { query: "", answer: "", wiki_citations: [], citations: [] };

// Turn inline [n] markers in the answer into links to source n's passage in the reader.
function renderAnswer(answer: string, citations: Citation[]) {
  return answer.split(/(\[\d+\])/g).map((tok, i) => {
    const m = tok.match(/^\[(\d+)\]$/);
    if (!m) return <span key={i}>{tok}</span>;
    const c = citations.find((x) => x.n === Number(m[1]));
    if (!c) return <span key={i}>{tok}</span>;
    return (
      <Link key={i} className="cite-mark" to={sourcePath(c)}>
        {tok}
      </Link>
    );
  });
}

export default function Chat() {
  const [params, setParams] = useSearchParams();
  const q = params.get("q") ?? "";
  const [draft, setDraft] = useState(q);
  const { data, error, loading } = useFetch(
    () => (q ? api.chat(q) : Promise.resolve(EMPTY)),
    `chat:${q}`,
  );

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
          <div className="rule-head"><h2>Answer</h2></div>
          <p className="answer-body">{renderAnswer(data.answer, data.citations)}</p>

          {data.wiki_citations.length > 0 && (
            <>
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
            </>
          )}

          {data.citations.length > 0 && (
            <>
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
            </>
          )}
        </article>
      )}
    </div>
  );
}
