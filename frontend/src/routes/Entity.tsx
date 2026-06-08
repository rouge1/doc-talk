import { Link, useParams } from "react-router-dom";
import { api } from "../api";
import { useFetch } from "../useFetch";

export default function Entity() {
  const { stem = "" } = useParams();
  const { data, error, loading } = useFetch(() => api.entity(stem), `entity:${stem}`);

  if (loading) return <div className="loading">Retrieving folio…</div>;
  if (error || !data) return <div className="empty">Entity not found in the archive.</div>;

  return (
    <div className="rise">
      <div className="crumbs"><Link to="/wiki">Wiki</Link> &nbsp;/&nbsp; {data.type}</div>

      <header className="folio">
        <span className="badge">{data.type} · {data.sources} source{data.sources !== 1 ? "s" : ""}</span>
        <h1>{data.name}</h1>
        {data.aliases.length > 0 && <div className="alias">also: {data.aliases.join(" · ")}</div>}
      </header>

      <div className="rule-head"><h2>Claims</h2><span className="count tnum">{data.claims.length}</span></div>
      {data.claims.map((c, i) => (
        <div className={`claim ${c.status !== "active" ? "contradicted" : ""}`} key={i}>
          <span className="idx tnum">{String(i + 1).padStart(2, "0")}</span>
          <span className="body">{c.text}</span>
          {c.sources.length > 0 && <span className="src">↳ {c.sources.join(" · ")}</span>}
        </div>
      ))}

      {data.related.length > 0 && (
        <>
          <div className="rule-head"><h2>Related</h2></div>
          <div className="related">
            {data.related.map((r) => (
              <Link className="chip" key={r.stem} to={`/wiki/entity/${r.stem}`}>{r.name}</Link>
            ))}
          </div>
        </>
      )}
    </div>
  );
}
