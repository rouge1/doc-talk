import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { api, type JobsData } from "../api";

// Live ingest dashboard: poll every 5s and update in place (no loading flicker between ticks).
export default function Jobs() {
  const [data, setData] = useState<JobsData | null>(null);
  const [failed, setFailed] = useState(false);

  useEffect(() => {
    let alive = true;
    const load = () =>
      api.jobs().then((d) => alive && setData(d)).catch(() => alive && setFailed(true));
    load();
    const id = setInterval(load, 5000);
    return () => {
      alive = false;
      clearInterval(id);
    };
  }, []);

  if (failed) return <div className="empty">Couldn't reach the ingest ledger.</div>;
  if (!data) return <div className="loading">Reading the ledger…</div>;

  const running = data.totals.running > 0;

  return (
    <div className="rise">
      <section className="hero compact">
        <div className="kicker">Resumable DAG · per-stage ledger</div>
        <h1 className="display">Ingest{running && <span className="live mono"> ● live</span>}</h1>
      </section>

      <div className="ledger">
        <Tot n={data.totals.done} l="Done" cls="s-done" />
        <Tot n={data.totals.running} l="Running" cls="s-running" />
        <Tot n={data.totals.pending} l="Pending" cls="s-pending" />
        <Tot n={data.totals.error} l="Error" cls="s-error" />
      </div>

      <div className="rule-head"><h2>Sources</h2><span className="count tnum">{data.files.length}</span></div>
      <div className="job-list">
        {data.files.map((f) => (
          <div className="job-row" key={f.hash}>
            <div className="job-head">
              <Link to={`/doc/${f.hash}`} className="job-name">{f.name}</Link>
              <span className={`job-state s-${f.state}`}>{f.state}</span>
              <span className="job-count mono tnum">{f.done}/{f.total}</span>
            </div>
            <div className="stage-strip" title={f.stages.map((s) => `${s.name}: ${s.status}`).join("\n")}>
              {f.stages.map((s) => (
                <span key={s.name} className={`seg s-${s.status}`} title={`${s.name} · ${s.status}`} />
              ))}
            </div>
          </div>
        ))}
      </div>

      {data.errors.length > 0 && (
        <>
          <div className="rule-head"><h2>Errors</h2><span className="count tnum">{data.errors.length}</span></div>
          {data.errors.map((e, i) => (
            <div className="card err-card" key={i}>
              <div className="mono err-head">{e.name} · {e.stage}</div>
              <div className="err-msg">{e.error || "(no message)"}</div>
              <div className="muted err-hint mono">retry: doctalk ingest &lt;file&gt; (re-runs only non-done stages)</div>
            </div>
          ))}
        </>
      )}
    </div>
  );
}

function Tot({ n, l, cls }: { n: number; l: string; cls: string }) {
  return (
    <div className="stat">
      <div className={`n tnum ${cls}`}>{n}</div>
      <div className="l">{l}</div>
    </div>
  );
}
