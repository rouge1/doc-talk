import { Link, useParams } from "react-router-dom";
import { api, chapterPath } from "../api";
import { useFetch } from "../useFetch";

export default function Doc() {
  const { hash = "" } = useParams();
  const { data, error, loading } = useFetch(() => api.doc(hash), `doc:${hash}`);

  if (loading) return <div className="loading">Pulling the volume…</div>;
  if (error || !data) return <div className="empty">Document not found in the archive.</div>;

  return (
    <div className="rise">
      <div className="crumbs"><Link to="/">Library</Link> &nbsp;/&nbsp; {data.format}</div>
      <header className="folio">
        <span className="badge">{data.format} · {data.chapters.length} section{data.chapters.length !== 1 ? "s" : ""}</span>
        <h1>{data.name}</h1>
      </header>

      <div className="rule-head"><h2>Contents</h2></div>
      {data.chapters.length === 0 ? (
        <div className="empty">No detected outline for this document.</div>
      ) : (
        <ol className="outline">
          {data.chapters.map((c) => (
            <li key={c.id} style={{ marginLeft: `${(c.level - 1) * 1.4}rem` }}>
              <Link to={chapterPath(hash, data.format, c)}>{c.title}</Link>
              <span className="leader" />
              <span className="pageno mono tnum">p.{c.page}</span>
            </li>
          ))}
        </ol>
      )}
    </div>
  );
}
