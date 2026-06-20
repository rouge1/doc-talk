import { useState } from "react";
import {
  api,
  getAdminToken,
  setAdminToken,
  type CollisionPlan,
  type Findings,
} from "../api";
import { useFetch } from "../useFetch";

// The operator loop, on the web: a read-only health dashboard (lint + audit) plus the one fully
// built action — the slug-collision batch heal (review the plan, then Apply). Reads are open;
// the Apply POST is gated server-side by DOCTALK_ADMIN_TOKEN (sent from the field at the bottom).
// Prune / reindex / lint-fix land here next, behind the same gate.
export default function Maintenance() {
  const [tick, setTick] = useState(0);
  const refresh = () => setTick((t) => t + 1);
  const lint = useFetch<Findings>(() => api.maintenanceLint(), `lint-${tick}`);
  const audit = useFetch<Findings>(() => api.maintenanceAudit(), `audit-${tick}`);
  const plan = useFetch<CollisionPlan>(() => api.slugCollisions(), `plan-${tick}`);

  const [applying, setApplying] = useState(false);
  const [msg, setMsg] = useState<string | null>(null);

  const apply = async () => {
    setApplying(true);
    setMsg(null);
    try {
      const res = await api.applyCollisions();
      setMsg(
        `Merged ${res.merged} collision${res.merged === 1 ? "" : "s"}` +
          (res.sha ? ` · ${res.sha.slice(0, 8)}` : "") +
          ".",
      );
      refresh();
    } catch (e) {
      setMsg(
        String(e).includes("401")
          ? "Admin token required — set it below, then retry."
          : `Failed: ${e}`,
      );
    } finally {
      setApplying(false);
    }
  };

  const total = plan.data
    ? plan.data.mergeable.length + plan.data.skipped.length
    : null;

  return (
    <div className="rise">
      <section className="hero compact">
        <div className="kicker">lint · heal · merge · prune</div>
        <h1 className="display">Maintenance</h1>
      </section>

      <div className="ledger">
        <Stat n={lint.data?.total} l="Flagged" />
        <Stat
          n={plan.data ? plan.data.mergeable.length : undefined}
          l="To merge"
          cls={plan.data && plan.data.mergeable.length > 0 ? "s-running" : ""}
        />
        <Stat
          n={audit.data?.total}
          l="Drift"
          cls={audit.data && audit.data.total > 0 ? "s-error" : "s-done"}
        />
      </div>

      <FindingsBlock title="Lint" state={lint} />
      <FindingsBlock title="Audit" state={audit} />

      <div className="rule-head">
        <h2>Slug collisions</h2>
        {total !== null && <span className="count tnum">{total}</span>}
      </div>
      {plan.loading && <div className="loading">Planning the merges…</div>}
      {plan.error && <div className="empty">Couldn't reach the planner.</div>}
      {plan.data && total === 0 && (
        <div className="empty">No slug collisions — the wiki is clean.</div>
      )}
      {plan.data && plan.data.mergeable.length > 0 && (
        <div className="merge-plan">
          <div className="plan-head">
            <span className="mono muted">{plan.data.mergeable.length} safe to merge</span>
            <button className="action" disabled={applying} onClick={apply}>
              {applying ? "Merging…" : "Apply merges"}
            </button>
          </div>
          {plan.data.mergeable.map((m, i) => (
            <div className="merge-row" key={i}>
              <span className="m-from">{m.src.name}</span>
              <span className="m-arrow mono">→</span>
              <span className="m-into">{m.dst.name}</span>
              <span className="m-type mono">{m.dst.type}</span>
            </div>
          ))}
        </div>
      )}
      {plan.data && plan.data.skipped.length > 0 && (
        <div className="merge-plan skipped">
          <div className="plan-head mono muted">{plan.data.skipped.length} left manual</div>
          {plan.data.skipped.map((m, i) => (
            <div className="merge-row" key={i}>
              <span className="m-from">{m.src.name}</span>
              <span className="m-arrow mono">~</span>
              <span className="m-into">{m.dst.name}</span>
              <span className="m-reason muted">{m.reason}</span>
            </div>
          ))}
        </div>
      )}
      {msg && <div className="action-msg mono">{msg}</div>}

      <AdminToken />
    </div>
  );
}

function Stat({ n, l, cls = "" }: { n: number | undefined; l: string; cls?: string }) {
  // Mirrors the Ingest ledger: a big serif number + mono label, the dashboard's at-a-glance thesis.
  return (
    <div className="stat">
      <div className={`n tnum ${cls}`}>{n ?? "·"}</div>
      <div className="l">{l}</div>
    </div>
  );
}

function FindingsBlock({
  title,
  state,
}: {
  title: string;
  state: { data: Findings | null; error: string | null; loading: boolean };
}) {
  return (
    <>
      <div className="rule-head">
        <h2>{title}</h2>
        {state.data && <span className="count tnum">{state.data.total}</span>}
      </div>
      {state.loading && <div className="loading">Checking…</div>}
      {state.error && <div className="empty">Couldn't run {title.toLowerCase()}.</div>}
      {state.data && state.data.total === 0 && (
        <div className="empty">Clean — nothing flagged.</div>
      )}
      {state.data && state.data.total > 0 && (
        <div className="finding-groups">
          {state.data.groups.map((g) => (
            <details className="finding-group" key={g.kind}>
              <summary>
                <span className="fg-kind mono">{g.kind.replace(/_/g, " ")}</span>
                <span className="fg-count tnum">{g.count}</span>
              </summary>
              <ul className="fg-items">
                {g.items.slice(0, 50).map((it, i) => (
                  <li key={i}>
                    {it.ref && <span className="fg-ref mono">{it.ref}</span>} {it.detail}
                  </li>
                ))}
                {g.items.length > 50 && (
                  <li className="muted">…and {g.items.length - 50} more</li>
                )}
              </ul>
            </details>
          ))}
        </div>
      )}
    </>
  );
}

function AdminToken() {
  const [val, setVal] = useState(getAdminToken());
  const [saved, setSaved] = useState(false);
  return (
    <div className="admin-token">
      <div className="rule-head">
        <h2>Admin token</h2>
      </div>
      <p className="muted at-note">
        Needed only when the server sets <code>DOCTALK_ADMIN_TOKEN</code>. Kept in this browser and
        sent as a header for mutating actions.
      </p>
      <div className="token-row">
        <input
          type="password"
          value={val}
          placeholder="admin token"
          onChange={(e) => {
            setVal(e.target.value);
            setSaved(false);
          }}
        />
        <button
          className="action"
          onClick={() => {
            setAdminToken(val.trim());
            setSaved(true);
          }}
        >
          {saved ? "Saved" : "Save"}
        </button>
      </div>
    </div>
  );
}
