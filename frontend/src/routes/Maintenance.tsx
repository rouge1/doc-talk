import { useEffect, useRef, useState } from "react";
import {
  api,
  getAdminToken,
  setAdminToken,
  type CollisionPlan,
  type Findings,
  type RecentBatch,
} from "../api";
import { useFetch } from "../useFetch";

// Maintenance, told as a case file. Each issue is worked in four acts — what's wrong, why it
// matters, what fixing it would do (a prediction), and, once you act, what actually happened
// (the prediction, verified, with an Undo). The slug-collision heal is the one fully-built remedy,
// so it carries the full narrative; Lint and Audit stay lighter (the finding + a plain diagnosis,
// no button — those heals aren't built yet). Reads are open; Apply/Undo are gated server-side by
// DOCTALK_ADMIN_TOKEN, sent from the field at the bottom.

// A live receipt: the prediction captured at Apply time, paired with what the server reported back.
// Held in state only for the session that applied it — a reload falls back to the durable recent batch.
interface LiveReceipt {
  sha: string | null;
  predicted: { src: string; dst: string }[]; // the pairs we said would merge (act ③'s contract)
  // folded entity -> the survivor's *final* name. Keyed by src because the merge may rename the
  // survivor to a cleaner title, so the src is the only stable identity to verify a row against.
  appliedBySrc: Map<string, string>;
  merged: number;
}

export default function Maintenance() {
  const [tick, setTick] = useState(0);
  const refresh = () => setTick((t) => t + 1);
  const plan = useFetch<CollisionPlan>(() => api.slugCollisions(), `plan-${tick}`);
  const recent = useFetch<RecentBatch>(() => api.recentMerges(), `recent-${tick}`);
  const lint = useFetch<Findings>(() => api.maintenanceLint(), `lint-${tick}`);
  const audit = useFetch<Findings>(() => api.maintenanceAudit(), `audit-${tick}`);

  const [live, setLive] = useState<LiveReceipt | null>(null);
  const [busy, setBusy] = useState<null | "apply" | "undo">(null);
  const [note, setNote] = useState<string | null>(null);

  const fail = (e: unknown) =>
    setNote(
      String(e).includes("401")
        ? "This needs the admin token — set it below, then try again."
        : `Couldn't finish: ${e}`,
    );

  const apply = async () => {
    if (!plan.data) return;
    setBusy("apply");
    setNote(null);
    try {
      const predicted = plan.data.mergeable.map((m) => ({ src: m.src.name, dst: m.dst.name }));
      const res = await api.applyCollisions();
      setLive({
        sha: res.sha,
        predicted,
        appliedBySrc: new Map(res.applied.map((a) => [a.src, a.dst])),
        merged: res.merged,
      });
      refresh();
    } catch (e) {
      fail(e);
    } finally {
      setBusy(null);
    }
  };

  const undo = async (sha: string) => {
    setBusy("undo");
    setNote(null);
    try {
      const res = await api.undoMerge(sha);
      setLive(null);
      setNote(`Reverted ${res.count} merge${res.count === 1 ? "" : "s"} — back to where we started.`);
      refresh();
    } catch (e) {
      fail(e);
    } finally {
      setBusy(null);
    }
  };

  return (
    <div className="rise maint">
      <section className="hero compact">
        <div className="kicker">lint · heal · merge · prune</div>
        <h1 className="display">Maintenance</h1>
        <p>
          Where the wiki drifts — duplicate pages, claims with no source, dead links. Each one is a
          case: see what's wrong, why it matters, and fix it. Every change is reversible.
        </p>
      </section>

      {/* vitals strip — same ledger Library and Ingest open with, so the operator pages read alike */}
      <div className="ledger">
        <Stat n={lint.data?.total} l="Flagged" />
        <Stat
          n={plan.data?.mergeable.length}
          l="To merge"
          cls={plan.data && plan.data.mergeable.length > 0 ? "s-running" : "s-done"}
        />
        <Stat
          n={audit.data?.total}
          l="Drift"
          cls={audit.data && audit.data.total > 0 ? "s-error" : "s-done"}
        />
      </div>

      <CollisionCase
        plan={plan}
        recent={recent.data ?? null}
        live={live}
        busy={busy}
        onApply={apply}
        onUndo={undo}
      />
      {note && <div className="action-msg mono">{note}</div>}

      <LighterSection title="Lint" sub="health check" state={lint} />
      <LighterSection title="Audit" sub="wiki ↔ truth" state={audit} />

      <AdminToken />
    </div>
  );
}

// One ledger cell — big serif number + mono label, state-coloured. Shared idiom with Ingest/Library.
function Stat({ n, l, cls = "" }: { n: number | undefined; l: string; cls?: string }) {
  return (
    <div className="stat">
      <div className={`n tnum ${cls}`}>{n ?? "·"}</div>
      <div className="l">{l}</div>
    </div>
  );
}

// --- the slug-collision case (the four acts) -----------------------------------------------------

function CollisionCase({
  plan,
  recent,
  live,
  busy,
  onApply,
  onUndo,
}: {
  plan: { data: CollisionPlan | null; error: string | null; loading: boolean };
  recent: RecentBatch | null;
  live: LiveReceipt | null;
  busy: null | "apply" | "undo";
  onApply: () => void;
  onUndo: (sha: string) => void;
}) {
  if (plan.loading && !plan.data)
    return <Case state="clean" title="Slug collisions" dek="Reading the wiki…" />;
  if (plan.error || !plan.data)
    return (
      <Case state="open" title="Slug collisions" dek="Couldn't reach the planner — is the API up?" />
    );

  const { mergeable, skipped } = plan.data;

  // 1) Just applied this session → the full verified receipt, animated.
  if (live) {
    return (
      <ResolvedCase
        pairs={live.predicted}
        appliedBySrc={live.appliedBySrc}
        merged={live.merged}
        predictedCount={live.predicted.length}
        sha={live.sha}
        skipped={skipped}
        busy={busy}
        onUndo={onUndo}
        animate
      />
    );
  }

  // 2) Work to do → the briefing (acts ① ② ③).
  if (mergeable.length > 0) {
    const before = mergeable.length + skipped.length; // every colliding pair today
    const after = skipped.length; // what the heal leaves behind — the human-judgement cases
    return (
      <Case
        state="open"
        title="Slug collisions"
        dek={`${mergeable.length} duplicate ${mergeable.length === 1 ? "page" : "pages"} would be overwritten, the original lost.`}
        action={
          <button className="action" disabled={busy !== null} onClick={onApply}>
            {busy === "apply"
              ? "Merging…"
              : `Apply ${mergeable.length} merge${mergeable.length === 1 ? "" : "s"}`}
          </button>
        }
      >
        <Act n="One" label="What's wrong">
          <p className="act-lede">
            These pairs each resolve to the <em>same</em> page filename — two entities, one file.
          </p>
          <ClashList pairs={mergeable.map((m) => ({ src: m.src.name, dst: m.dst.name, slug: m.dst.stem }))} />
        </Act>

        <Act n="Two" label="Why it's a problem">
          <p className="act-prose">
            Two entities can't own one file. Whichever the synthesizer writes second overwrites the
            first, so a page silently vanishes and every link from other pages breaks. Folding the
            duplicate into the original leaves one page holding both sets of claims — nothing lost.
          </p>
        </Act>

        <Act n="Three" label="What fixing it does">
          <div className="contract">
            <div className="contract-delta">
              <span className="delta-eyebrow mono">slug collisions</span>
              <div className="delta-grid">
                <span className="contract-n tnum">{before}</span>
                <span className="delta-arrow">→</span>
                <span className="contract-n tnum">{after}</span>
                <span className="delta-sub mono">now</span>
                <span aria-hidden="true" />
                <span className="delta-sub mono">after this heal</span>
              </div>
            </div>
            <ul className="contract-ticks">
              <li>
                {mergeable.length} duplicate {mergeable.length === 1 ? "page folds" : "pages fold"} —
                every claim kept on the survivor
              </li>
              {skipped.length > 0 && (
                <li>
                  the {skipped.length} that {skipped.length === 1 ? "remains is" : "remain are"}{" "}
                  genuinely different — a human's call
                </li>
              )}
              <li>reversible — one button undoes the whole batch</li>
            </ul>
          </div>
          <ManualRemainder skipped={skipped} />
        </Act>
      </Case>
    );
  }

  // 3) Nothing left to merge, but a batch is on record → the durable receipt + Undo (survives reload).
  if (recent?.sha) {
    return (
      <ResolvedCase
        pairs={recent.merges.map((m) => ({ src: m.src, dst: m.dst }))}
        appliedBySrc={new Map(recent.merges.map((m) => [m.src, m.dst]))}
        merged={recent.count}
        predictedCount={recent.count}
        sha={recent.sha}
        skipped={skipped}
        busy={busy}
        onUndo={onUndo}
        animate={false}
      />
    );
  }

  // 4) Clean — or only human-judgement cases remain.
  return (
    <Case
      state={skipped.length > 0 ? "open" : "clean"}
      title="Slug collisions"
      dek={
        skipped.length > 0
          ? "Nothing safe to auto-merge — only genuinely-different pairs remain."
          : "Every entity owns a unique page. Nothing to merge."
      }
    >
      {skipped.length > 0 && (
        <Act n="One" label="Left for a human">
          <p className="act-lede">
            The slugifier collides these, but they're really distinct — merging would conflate them.
          </p>
          <ManualRemainder skipped={skipped} bare />
        </Act>
      )}
    </Case>
  );
}

// Act ④, both live (animated, full predict→verify) and rehydrated (static, durable).
function ResolvedCase({
  pairs,
  appliedBySrc,
  merged,
  predictedCount,
  sha,
  skipped,
  busy,
  onUndo,
  animate,
}: {
  pairs: { src: string; dst: string }[];
  appliedBySrc: Map<string, string>;
  merged: number;
  predictedCount: number;
  sha: string | null;
  skipped: CollisionPlan["skipped"];
  busy: null | "apply" | "undo";
  onUndo: (sha: string) => void;
  animate: boolean;
}) {
  // Verify each predicted merge by its folded entity (src); show the survivor's final name, which the
  // merge may have cleaned up (e.g. channel map -> Channel Map). A src with no applied entry held back.
  const rows = pairs.map((p) => {
    const finalDst = appliedBySrc.get(p.src);
    return { src: p.src, dst: finalDst ?? p.dst, ok: finalDst !== undefined };
  });
  const holdouts = rows.filter((r) => !r.ok);
  const allLanded = holdouts.length === 0;
  const shown = useCountUp(merged, animate);

  return (
    <Case
      state="resolved"
      title="Slug collisions"
      dek={allLanded ? "Healed — the duplicates are folded into their originals." : "Partly healed — some pairs held back."}
    >
      <Act n="Four" label="What happened">
        <div className={`verify ${allLanded ? "ok" : "warn"}`}>
          <span className="verify-mark">{allLanded ? "✓" : "⚠"}</span>
          <span className="verify-line">
            {animate ? (
              <>
                We predicted <span className="mono">{predictedCount}</span>.{" "}
                <strong className="tnum">{shown}</strong>{" "}
                {merged === 1 ? "merged" : "merged"}
                {allLanded ? " — exactly as called." : ` of ${predictedCount} — ${holdouts.length} held back.`}
              </>
            ) : (
              <>
                <strong className="tnum">{merged}</strong> {merged === 1 ? "merge" : "merges"} on record
                in the last batch.
              </>
            )}
          </span>
        </div>

        <ul className={`receipt ${animate ? "settling" : ""}`}>
          {rows.map((r, i) => (
            <li
              className={`receipt-row ${r.ok ? "ok" : "held"}`}
              key={i}
              style={animate ? { animationDelay: `${0.12 + i * 0.05}s` } : undefined}
            >
              <span className="r-check">{r.ok ? "✓" : "⚠"}</span>
              <span className="r-from">{r.src}</span>
              <span className="r-arrow mono">{r.ok ? "→" : "✗"}</span>
              <span className="r-into">{r.dst}</span>
              {!r.ok && <span className="r-note muted">held back</span>}
            </li>
          ))}
        </ul>

        <div className="receipt-foot">
          {sha && <span className="sha mono">committed {sha.slice(0, 8)}</span>}
          {sha && (
            <button className="undo" disabled={busy !== null} onClick={() => onUndo(sha)}>
              {busy === "undo" ? "Undoing…" : "Undo this batch"}
            </button>
          )}
        </div>

        <ManualRemainder skipped={skipped} />
      </Act>
    </Case>
  );
}

// --- case + act scaffolding ----------------------------------------------------------------------

function Case({
  state,
  title,
  dek,
  action,
  children,
}: {
  state: "open" | "resolved" | "clean";
  title: string;
  dek: string;
  action?: React.ReactNode;
  children?: React.ReactNode;
}) {
  const stamp = { open: "Open", resolved: "Resolved", clean: "Clear" }[state];
  return (
    <section className={`case ${state}`}>
      <header className="case-head">
        <div>
          <div className="kicker">heal · merge</div>
          <h2 className="case-title">{title}</h2>
          <p className="case-dek">{dek}</p>
        </div>
        <div className="case-head-right">
          <span className={`case-stamp ${state}`}>{stamp}</span>
          {action}
        </div>
      </header>
      {children}
    </section>
  );
}

function Act({ n, label, children }: { n: string; label: string; children: React.ReactNode }) {
  return (
    <div className="act">
      <div className="act-rail">
        <span className="act-num">{n}</span>
        <span className="act-label">{label}</span>
      </div>
      <div className="act-body">{children}</div>
    </div>
  );
}

function ClashList({ pairs }: { pairs: { src: string; dst: string; slug: string | null }[] }) {
  return (
    <ul className="clash-list">
      {pairs.map((p, i) => (
        <li className="clash" key={i}>
          <span className="clash-a">{p.src}</span>
          <span className="clash-b">{p.dst}</span>
          <span className="clash-slug mono">{p.slug ? `${p.slug}.md` : "—"}</span>
        </li>
      ))}
    </ul>
  );
}

function ManualRemainder({
  skipped,
  bare = false,
}: {
  skipped: CollisionPlan["skipped"];
  bare?: boolean;
}) {
  if (skipped.length === 0) return null;
  return (
    <div className="remainder">
      {!bare && (
        <div className="remainder-head mono">
          {skipped.length} left for a human
        </div>
      )}
      {skipped.map((m, i) => (
        <div className="remainder-row" key={i}>
          <span className="r-from">{m.src.name}</span>
          <span className="r-arrow mono">~</span>
          <span className="r-into">{m.dst.name}</span>
          <span className="r-reason muted">{m.reason}</span>
        </div>
      ))}
    </div>
  );
}

// --- lighter sections (Lint / Audit): the finding + a plain diagnosis, no remedy yet -------------

const DIAGNOSIS: Record<string, string> = {
  orphan: "No page links here — nothing in the wiki leads to it. You'd only find it by guessing the URL.",
  unsupported_claim: "A claim with no source chunk behind it. It can't be checked against the documents, so it can't be trusted.",
  missing_page: "Mentioned across sources but never got a page written — a gap where a page should be.",
  deleted_page: "The catalog points at a page file that's gone from disk. Run wiki-lint --fix to reconcile the two.",
  unresolved: "A provisional page the resolver couldn't place. It's waiting on a human's same-or-different call.",
  slug_collision: "Two entities share one page filename — handled in the case above, which folds the safe ones.",
  duplicate: "Looks like a near-duplicate of another entity. A merge would join their claims onto one page.",
};
const diagnose = (kind: string) =>
  DIAGNOSIS[kind] ?? "Flagged by the linter — review the evidence below.";

function LighterSection({
  title,
  sub,
  state,
}: {
  title: string;
  sub: string;
  state: { data: Findings | null; error: string | null; loading: boolean };
}) {
  return (
    <section className="lighter">
      <div className="rule-head">
        <h2>{title}</h2>
        <span className="lighter-sub mono">{sub}</span>
        {state.data && <span className="count tnum">{state.data.total}</span>}
      </div>
      {state.loading && <div className="loading">Checking…</div>}
      {state.error && <div className="empty">Couldn't run {title.toLowerCase()}.</div>}
      {state.data && state.data.total === 0 && <div className="empty">Clean — nothing flagged.</div>}
      {state.data && state.data.total > 0 && (
        <div className="findings">
          {state.data.groups.map((g) => (
            <div className="finding" key={g.kind}>
              <div className="finding-top">
                <span className="finding-kind">{g.kind.replace(/_/g, " ")}</span>
                <span className="finding-count tnum">{g.count}</span>
              </div>
              <p className="finding-why">{diagnose(g.kind)}</p>
              <details className="finding-ev">
                <summary>show {g.count === 1 ? "it" : `all ${g.count}`}</summary>
                <ul>
                  {g.items.slice(0, 50).map((it, i) => (
                    <li key={i}>
                      {it.ref && <span className="fg-ref mono">{it.ref}</span>} {it.detail}
                    </li>
                  ))}
                  {g.items.length > 50 && <li className="muted">…and {g.items.length - 50} more</li>}
                </ul>
              </details>
            </div>
          ))}
        </div>
      )}
    </section>
  );
}

// --- count-up: the one number that ticks (the merge count, on Apply). Respects reduced-motion. ---

function useCountUp(target: number, run: boolean): number {
  const [v, setV] = useState(run ? 0 : target);
  const raf = useRef(0);
  useEffect(() => {
    const reduce = window.matchMedia?.("(prefers-reduced-motion: reduce)").matches;
    if (!run || reduce) {
      setV(target);
      return;
    }
    const start = performance.now();
    const step = (now: number) => {
      const t = Math.min(1, (now - start) / 700);
      setV(Math.round(target * (1 - Math.pow(1 - t, 3)))); // ease-out cubic
      if (t < 1) raf.current = requestAnimationFrame(step);
    };
    raf.current = requestAnimationFrame(step);
    return () => cancelAnimationFrame(raf.current);
  }, [target, run]);
  return v;
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
        sent as a header for Apply and Undo.
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
