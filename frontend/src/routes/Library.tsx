import { Link } from "react-router-dom";
import { api } from "../api";
import { useFetch } from "../useFetch";
import Plate from "../components/Plate";

const callno = (i: number) => `№ ${String(i + 1).padStart(3, "0")}`;

export default function Library() {
  const stats = useFetch(api.stats, "stats");
  const lib = useFetch(api.library, "library");

  return (
    <div className="rise">
      <section className="hero hero-grid">
        <div>
          <div className="kicker">Local knowledge archive</div>
          <h1 className="display">Everything you dropped,<br />read and reasoned about.</h1>
          <p>
            doctalk ingests your documents and photographs, then compiles them into a navigable,
            source-traced wiki you can search, question, and browse.
          </p>
        </div>
        <Plate
          serial={`№ ${String(stats.data?.documents ?? 0).padStart(3, "0")}`}
          lines={["Local archive", "On-device", "No cloud · no leaks"]}
        />
      </section>

      {stats.data && (
        <div className="ledger rise">
          <Stat n={stats.data.documents} l="Documents" />
          <Stat n={stats.data.images} l="Images" />
          <Stat n={stats.data.entities} l="Entities" />
          <Stat n={stats.data.claims} l="Claims" />
          <Stat n={stats.data.queries} l="Queries" />
        </div>
      )}

      <div className="rule-head">
        <h2>Sources</h2>
        <span className="count tnum">{lib.data ? lib.data.sources.length : "—"}</span>
      </div>

      {lib.error && <div className="empty">Couldn't reach the archive. Is the server running?</div>}
      {lib.loading && <Skeletons />}
      {lib.data && lib.data.sources.length === 0 && (
        <div className="empty">No sources yet — run <span className="mono">doctalk ingest &lt;file&gt;</span>.</div>
      )}

      <div className="catalog">
        {lib.data?.sources.map((sc, i) => (
          <Link key={sc.stem} className="entry rise" style={{ animationDelay: `${i * 40}ms` }}
                to={`/wiki/source/${sc.stem}`}>
            <span className="callno">{callno(i)}</span>
            <span>
              <div className="title">{sc.title}</div>
              <div className="sub">{sc.format} · synthesis profile</div>
            </span>
            <span className="meta tnum">{sc.chapters} ch · {sc.entities} ent · {sc.claims} claims</span>
          </Link>
        ))}
      </div>

      <div className="rule-head">
        <h2>Documents</h2>
        <span className="count tnum">{lib.data ? lib.data.documents.length : "—"}</span>
      </div>
      <div className="catalog">
        {lib.data?.documents.map((d, i) => (
          <Link key={d.hash} className="entry rise" style={{ animationDelay: `${i * 40}ms` }}
                to={`/doc/${d.hash}`}>
            <span className="callno">{callno(i)}</span>
            <span>
              <div className="title">{d.name}</div>
              <div className="sub">{d.format}</div>
            </span>
            <span className="meta">{d.chapters} ch · {d.chunks} chunks</span>
          </Link>
        ))}
      </div>

      {lib.data && lib.data.images > 0 && (
        <p className="muted" style={{ marginTop: "1.6rem" }}>
          + {lib.data.images} image(s) in the <Link to="/gallery">gallery →</Link>
        </p>
      )}
    </div>
  );
}

function Stat({ n, l }: { n: number; l: string }) {
  return (
    <div className="stat">
      <div className="n tnum">{n}</div>
      <div className="l">{l}</div>
    </div>
  );
}

function Skeletons() {
  return (
    <div className="catalog">
      {[0, 1, 2].map((i) => (
        <div key={i} className="entry"><span /><span className="skeleton" style={{ width: "60%" }} /><span /></div>
      ))}
    </div>
  );
}
