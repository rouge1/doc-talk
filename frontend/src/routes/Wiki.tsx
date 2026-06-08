import { Link } from "react-router-dom";
import { api } from "../api";
import { useFetch } from "../useFetch";

export default function Wiki() {
  const { data, error, loading } = useFetch(api.wiki, "wiki");

  if (loading) return <div className="loading">Opening the wiki…</div>;
  if (error || !data) return <div className="empty">Couldn't load the wiki.</div>;

  return (
    <div className="rise">
      <section className="hero">
        <div className="kicker">The synthesis layer</div>
        <h1 className="display">The wiki</h1>
        <p>
          Entities and claims distilled from your documents — interlinked, deduplicated across
          sources, and traced back to the exact passage they came from.
        </p>
      </section>

      <div className="ledger">
        <div className="stat"><div className="n tnum">{data.totals.entities}</div><div className="l">Entities</div></div>
        <div className="stat"><div className="n tnum">{data.totals.claims}</div><div className="l">Claims</div></div>
        <div className="stat"><div className="n tnum">{data.totals.queries}</div><div className="l">Queries</div></div>
      </div>

      {data.reviews > 0 && (
        <div className="banner">⚖ {data.reviews} resolution{data.reviews !== 1 ? "s" : ""} awaiting review</div>
      )}

      {data.groups.map((g) => (
        <section className="type-block" key={g.type}>
          <div className="rule-head">
            <h2>{g.type}</h2>
            <span className="count tnum">{g.entities.length}</span>
          </div>
          <div className="entity-grid">
            {g.entities.map((e) => (
              <div className="entity-item" key={e.name}>
                {e.stem ? <Link to={`/wiki/entity/${e.stem}`}>{e.name}</Link> : <span>{e.name}</span>}
                <div className="m">{e.claims} claim{e.claims !== 1 ? "s" : ""} · {e.sources} src</div>
              </div>
            ))}
          </div>
        </section>
      ))}

      {data.queries.length > 0 && (
        <section className="type-block">
          <div className="rule-head"><h2>Queries</h2><span className="count tnum">{data.queries.length}</span></div>
          <div className="entity-grid">
            {data.queries.map((q) => (
              <div className="entity-item" key={q.stem}>
                <Link to={`/wiki/query/${q.stem}`}>{q.title}</Link>
              </div>
            ))}
          </div>
        </section>
      )}
    </div>
  );
}
