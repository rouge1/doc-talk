import { Link, useNavigate, useParams, useSearchParams } from "react-router-dom";
import { api } from "../api";
import { useFetch } from "../useFetch";

export default function Entity() {
  const { stem = "" } = useParams();
  const navigate = useNavigate();
  const [params] = useSearchParams();
  // The unresolved-entities case links a no-candidate entity straight here (there's nothing to compare).
  // When it does, the back button mirrors the compare page's "Back to the list" — same pill, same words,
  // back to the same section — so drilling into an unresolved entity feels like the rest of the
  // maintenance flow rather than a detour into the wiki. Elsewhere it stays a plain "Back".
  const fromUnresolved = params.get("from") === "unresolved";
  const { data, error, loading } = useFetch(() => api.entity(stem), `entity:${stem}`);
  // Go back to wherever we came from — navigate(-1) returns to the list with its scroll intact, better
  // than a fresh #hash jump. The fallback covers a direct load with no in-app history.
  const goBack = () =>
    window.history.length > 1
      ? navigate(-1)
      : navigate(fromUnresolved ? "/maintenance#unresolved" : "/wiki");

  if (loading) return <div className="loading">Retrieving folio…</div>;
  if (error || !data) return <div className="empty">Entity not found in the archive.</div>;

  return (
    <div className="rise">
      <div className="folio-nav">
        <div className="crumbs"><Link to="/wiki">Wiki</Link> &nbsp;/&nbsp; {data.type}</div>
        <button type="button" className="back-pill" onClick={goBack}>
          {fromUnresolved ? "← Back to the list" : "← Back"}
        </button>
      </div>

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
