import { useEffect, useRef } from "react";
import { Link, useParams, useSearchParams } from "react-router-dom";
import { api } from "../api";
import { useFetch } from "../useFetch";

export default function Reader() {
  const { hash = "", chapterId = "" } = useParams();
  const [params] = useSearchParams();
  const focus = Number(params.get("focus")) || null;
  const focusRef = useRef<HTMLDivElement>(null);
  const { data, error, loading } = useFetch(
    () => api.chapter(hash, Number(chapterId)),
    `chap:${hash}:${chapterId}:${focus}`,
  );

  useEffect(() => {
    if (data && focusRef.current) {
      focusRef.current.scrollIntoView({ behavior: "smooth", block: "center" });
    }
  }, [data]);

  if (loading) return <div className="loading">Opening the leaf…</div>;
  if (error || !data) return <div className="empty">Section not found.</div>;

  return (
    <div className="rise reader">
      <div className="crumbs">
        <Link to={`/doc/${hash}`}>{data.doc_name}</Link> &nbsp;/&nbsp; section
      </div>
      <header className="folio">
        <span className="badge">Begins on p.{data.chapter.page}</span>
        <h1>{data.chapter.title}</h1>
      </header>

      <article className="leaf">
        {data.chunks.map((c) => {
          const hit = c.id === focus;
          return (
            <div key={c.id} ref={hit ? focusRef : undefined} className={`para ${hit ? "match" : ""}`}>
              <span className="pageno mono tnum">{c.page}</span>
              {hit && <span className="match-tag mono">matched passage ↓</span>}
              <p>{c.text}</p>
            </div>
          );
        })}
      </article>

      <nav className="leaf-nav mono">
        {data.nav.prev ? <Link to={`/doc/${hash}/chapter/${data.nav.prev}`}>← previous</Link> : <span />}
        {data.nav.next ? <Link to={`/doc/${hash}/chapter/${data.nav.next}`}>next →</Link> : <span />}
      </nav>
    </div>
  );
}
