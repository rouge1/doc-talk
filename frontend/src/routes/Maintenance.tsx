import { useEffect, useRef, useState } from "react";
import { Link, useLocation } from "react-router-dom";
import {
  api,
  getAdminToken,
  setAdminToken,
  type CollisionPlan,
  type DupBand,
  type DupPair,
  type DuplicatePlan,
  type FindingGroup,
  type Findings,
  type RecentBatch,
  type RecentSplits,
  type SplitEntity,
} from "../api";
import { useFetch } from "../useFetch";

// Which mutating action is in flight — every button disables while any one runs (one op at a time).
type Busy = null | "apply" | "undo" | "split" | "unsplit";

// An action's outcome, filed under the case it belongs to. Tone reads it at a glance: done (green),
// undone (a reversal, amber), error (oxblood).
type Note = { text: string; tone: "done" | "undone" | "error" } | null;

// The disambiguation half of the slug-collision case, bundled so it threads cleanly into the acts.
interface HumanWork {
  splits: RecentSplits | null;
  busy: Busy;
  onDisambiguate: () => void;
  onUndoSplit: (ids: number[]) => void;
}

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

// Every check the page knows about, in the reader's words — the "all clear" line names them all.
// `slug_collision` and `duplicate` get rich, hand-built cases; the rest render in one uniform anatomy.
const KIND_NAME: Record<string, string> = {
  slug_collision: "slug collisions",
  duplicate: "duplicate pages",
  unresolved: "unresolved entities",
  unsupported_claim: "unsupported claims",
  orphan: "orphan pages",
  missing_page: "missing pages",
  deleted_page: "dead page links",
  contradiction: "contradictions",
  unattested: "unattested entities",
  stale_query: "stale answers",
  catalog_drift: "catalog drift",
  dangling_source: "dangling sources",
};
const ALL_CHECKS = Object.keys(KIND_NAME);

// The uniform anatomy for the kinds without a bespoke case: what it needs, why it matters, how to fix,
// how to undo. `need` drives the ledger (anything but "fix" wants a human) and the eyebrow.
type Need = "decision" | "review" | "fix";
const CHECK: Record<string, { title: string; eyebrow: string; need: Need; why: string; fix: string; undo: string }> = {
  unresolved: {
    title: "Unresolved entities", eyebrow: "needs a human call", need: "decision",
    why: "The resolver couldn't tell if each is a new entity or another spelling of one it already has.",
    fix: "Open each and make the same-or-different call.",
    undo: "Nothing's changed yet — this is a read-only queue.",
  },
  contradiction: {
    title: "Contradictions", eyebrow: "needs a call", need: "decision",
    why: "Two sources make claims that can't both be true — flagged, never silently overwritten.",
    fix: "Read both citations, then keep, qualify, or retire one.",
    undo: "Read-only until you act.",
  },
  unsupported_claim: {
    title: "Unsupported claims", eyebrow: "needs review", need: "review",
    why: "A claim with no source chunk behind it can't be checked against the documents, so it can't be trusted.",
    fix: "Re-run synthesis for the source, or retire the claim.",
    undo: "Read-only — nothing removed yet.",
  },
  orphan: {
    title: "Orphan pages", eyebrow: "needs a link", need: "review",
    why: "No page links here, so nothing in the wiki leads to it — you'd only reach it by guessing the URL.",
    fix: "Link it from a related page, or prune it if it's noise.",
    undo: "Read-only until you act.",
  },
  unattested: {
    title: "Unattested entities", eyebrow: "needs review", need: "review",
    why: "No source attests this anymore — a leftover from a re-synthesis that dropped its claims.",
    fix: "Prune it with wiki-prune — a future mention brings it back.",
    undo: "Pruning is reversible.",
  },
  stale_query: {
    title: "Stale answers", eyebrow: "needs review", need: "review",
    why: "A filed answer cites entities that have gained claims since — it may now be out of date.",
    fix: "Re-ask the question to refresh the saved answer.",
    undo: "Read-only until you re-ask.",
  },
  missing_page: {
    title: "Missing pages", eyebrow: "ready to fix", need: "fix",
    why: "Mentioned across sources but never written — a gap where a page should be.",
    fix: "Run wiki-lint --fix to materialize the absent pages.",
    undo: "Each created page can be deleted.",
  },
  deleted_page: {
    title: "Dead page links", eyebrow: "ready to fix", need: "fix",
    why: "The catalog points at a page file that's gone from disk — the index and the disk disagree.",
    fix: "Run wiki-lint --fix to reconcile the catalog with disk.",
    undo: "Reconciliation is re-runnable.",
  },
  catalog_drift: {
    title: "Catalog drift", eyebrow: "ready to fix", need: "fix",
    why: "A page on disk the catalog doesn't know about, or the reverse — wiki and truth store disagree.",
    fix: "Run wiki-lint --fix to reconcile the two.",
    undo: "Reconciliation is re-runnable.",
  },
  dangling_source: {
    title: "Dangling sources", eyebrow: "needs review", need: "review",
    why: "A claim cites a source chunk that no longer exists — its provenance can't be followed.",
    fix: "Re-ingest the source, or retire the claim.",
    undo: "Read-only until you act.",
  },
};

// Every issue collapses into one of two action classes (a third, "clear", is the absence of both).
// `slug_collision` is mechanical (merge/split); `duplicate` is a judgment; the rest follow their need.
const classOf = (kind: string): "apply" | "decide" =>
  kind === "slug_collision" || CHECK[kind]?.need === "fix" ? "apply" : "decide";

export default function Maintenance() {
  const [tick, setTick] = useState(0);
  const refresh = () => setTick((t) => t + 1);
  const location = useLocation();
  const dupRef = useRef<HTMLDivElement>(null);
  const scrolledHash = useRef<string | null>(null);
  const plan = useFetch<CollisionPlan>(() => api.slugCollisions(), `plan-${tick}`);
  const recent = useFetch<RecentBatch>(() => api.recentMerges(), `recent-${tick}`);
  const splits = useFetch<RecentSplits>(() => api.recentSplits(), `splits-${tick}`);
  const dups = useFetch<DuplicatePlan>(() => api.duplicates(), `dups-${tick}`);
  const lint = useFetch<Findings>(() => api.maintenanceLint(), `lint-${tick}`);
  const audit = useFetch<Findings>(() => api.maintenanceAudit(), `audit-${tick}`);

  const [live, setLive] = useState<LiveReceipt | null>(null);
  const [busy, setBusy] = useState<Busy>(null);
  const [note, setNote] = useState<Note>(null);
  const [needsToken, setNeedsToken] = useState(false); // an action 401'd — surface the token field inline

  // Arriving from a compare via #duplicates: drop straight onto the band list, not the page top. The
  // duplicates plan is cached now, so it resolves *first* — if we scrolled then, the collision case
  // above would finish loading a beat later, grow, and shove the band list back off-screen (reads as a
  // jump to the top). So wait until everything above the band list has settled, then scroll once.
  useEffect(() => {
    const aboveSettled = !plan.loading && !recent.loading && !splits.loading && !dups.loading;
    if (
      location.hash === "#duplicates" &&
      aboveSettled &&
      dupRef.current &&
      scrolledHash.current !== location.key
    ) {
      scrolledHash.current = location.key; // once per arrival, even if later fetches re-render
      const reduce = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
      dupRef.current.scrollIntoView({ behavior: reduce ? "auto" : "smooth", block: "start" });
    }
  }, [location.hash, location.key, plan.loading, recent.loading, splits.loading, dups.loading]);

  const fail = (e: unknown) => {
    const is401 = String(e).includes("401");
    if (is401) setNeedsToken(true);
    setNote({
      text: is401
        ? "That action needs the admin token — add it below, then try again."
        : `Couldn't finish: ${e}`,
      tone: "error",
    });
  };

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
      setNote({
        text: `Reverted ${res.count} merge${res.count === 1 ? "" : "s"} — back to where we started.`,
        tone: "undone",
      });
      refresh();
    } catch (e) {
      fail(e);
    } finally {
      setBusy(null);
    }
  };

  // Disambiguation: give each genuinely-distinct collision its own page (no merge — nothing conflated).
  const disambiguate = async () => {
    setBusy("split");
    setNote(null);
    try {
      const res = await api.disambiguate();
      setNote({
        text:
          res.count === 0
            ? "Nothing to split — those pages already have unique slugs."
            : `Split ${res.count} ${res.count === 1 ? "page" : "pages"} onto ${res.count === 1 ? "its" : "their"} own slug.`,
        tone: "done",
      });
      refresh();
    } catch (e) {
      fail(e);
    } finally {
      setBusy(null);
    }
  };

  const undoSplit = async (ids: number[]) => {
    setBusy("unsplit");
    setNote(null);
    try {
      const res = await api.undoDisambiguate(ids);
      setNote({
        text: `Folded ${res.count} ${res.count === 1 ? "page" : "pages"} back onto the shared slug.`,
        tone: "undone",
      });
      refresh();
    } catch (e) {
      fail(e);
    } finally {
      setBusy(null);
    }
  };

  const human: HumanWork = {
    splits: splits.data ?? null,
    busy,
    onDisambiguate: disambiguate,
    onUndoSplit: undoSplit,
  };

  // Findings sort into the page's three classes: what only you can decide, what's mechanical to fix,
  // and what's clean. duplicate + slug_collision get rich, hand-built cases; the rest render in one
  // uniform anatomy; anything not flagged is named under "all clear". The ledger counts the classes.
  const groups = [...(lint.data?.groups ?? []), ...(audit.data?.groups ?? [])];
  const flagged = new Set(groups.map((g) => g.kind));
  const issueGroups = groups.filter((g) => CHECK[g.kind] && g.count > 0);
  const decideIssues = issueGroups.filter((g) => classOf(g.kind) === "decide");
  const applyIssues = issueGroups.filter((g) => classOf(g.kind) === "apply");
  const clearKinds = ALL_CHECKS.filter((k) => !flagged.has(k));

  const dupPairs = dups.data?.total ?? 0;
  const slugWork = !!plan.data && plan.data.mergeable.length + plan.data.skipped.length > 0;
  // Each ledger number is a count of the cards in its section below — click it to jump there. Each
  // fills from only the data it needs (findings for all three, plus duplicates / the slug plan), so a
  // stat clears its "·" the moment it can rather than waiting on the slowest of every fetch.
  const haveFindings = !!(lint.data && audit.data);
  const needYou = haveFindings && dups.data ? (dupPairs > 0 ? 1 : 0) + decideIssues.length : undefined;
  const readyToFix = haveFindings && plan.data ? (slugWork ? 1 : 0) + applyIssues.length : undefined;
  const clean = haveFindings ? clearKinds.length : undefined;

  return (
    <div className="rise maint">
      <section className="hero compact">
        <div className="kicker">lint · heal · merge · prune</div>
        <h1 className="display">Maintenance</h1>
        <p>
          Where the wiki drifts. Every issue sorts into three kinds — it <em>needs your call</em>, it's{" "}
          <em>ready to fix</em>, or it's <em>all clear</em> — and every change is reversible.
        </p>
      </section>

      {/* vitals strip — each number counts the cards in its class below, and links to that section */}
      <div className="ledger">
        <Stat n={needYou} l="Needs your call" href="#decide" cls={needYou ? "s-running" : "s-done"} />
        <Stat n={readyToFix} l="Ready to fix" href="#ready" cls={readyToFix ? "s-running" : "s-done"} />
        <Stat n={clean} l="All clear" href="#clear" cls="s-done" />
      </div>

      {note && (
        <div className={`outcome ${note.tone}`} role="status">
          <span className="outcome-mark mono" aria-hidden="true">
            {note.tone === "error" ? "!" : note.tone === "undone" ? "↺" : "✓"}
          </span>
          <span className="outcome-text">{note.text}</span>
        </div>
      )}
      {needsToken && (
        <AdminPrompt
          onSaved={() => {
            setNeedsToken(false);
            setNote(null);
          }}
        />
      )}

      {/* Class 1 — only you can decide. Lead with it: this is the substance of the page. */}
      <ClassSection
        id="decide"
        n={needYou}
        title="Needs your call"
        blurb="A judgment only you can make — read the evidence, then decide."
        empty={needYou === 0 ? "Nothing waiting on a decision." : null}
      >
        {(!dups.data || dupPairs > 0) && (
          <div id="duplicates" ref={dupRef} style={{ scrollMarginTop: "5rem" }}>
            <DuplicatesCase state={dups} />
          </div>
        )}
        {decideIssues.map((g) => (
          <IssueCase key={g.kind} group={g} />
        ))}
      </ClassSection>

      {/* Class 2 — mechanical, one approval, always reversible. */}
      <ClassSection
        id="ready"
        n={readyToFix}
        title="Ready to fix"
        blurb="The fix is mechanical — the system knows the answer and just needs your go-ahead."
        empty={readyToFix === 0 ? "Nothing queued — no one-click fixes waiting." : null}
      >
        <SlugCase plan={plan} busy={busy} onApply={apply} human={human} />
        {applyIssues.map((g) => (
          <IssueCase key={g.kind} group={g} />
        ))}
      </ClassSection>

      {/* Class 3 — clean. Every check that found nothing; each would open a case above if it had. */}
      <ClassSection
        id="clear"
        n={clean}
        tone="good"
        title="All clear"
        blurb="Checks that found nothing this run — each would open its own case above if it did."
      >
        {clearKinds.length > 0 && (
          <p className="ac-list">{clearKinds.map((k) => KIND_NAME[k]).join(" · ")}</p>
        )}
      </ClassSection>

      <RecentActivity
        live={live}
        recent={recent.data ?? null}
        splits={splits.data ?? null}
        busy={busy}
        onUndo={undo}
        onUndoSplit={undoSplit}
      />
    </div>
  );
}

// One ledger cell — big serif number + mono label, state-coloured. Shared idiom with Ingest/Library.
// With `href` it's a link to the matching section below, so the number is a way in, not just a count.
function Stat({ n, l, cls = "", href }: { n: number | undefined; l: string; cls?: string; href?: string }) {
  const body = (
    <>
      <div className={`n tnum ${cls}`}>{n ?? "·"}</div>
      <div className="l">{l}</div>
    </>
  );
  return href ? (
    <a className="stat statlink" href={href}>
      {body}
    </a>
  ) : (
    <div className="stat">{body}</div>
  );
}

// A class section — one of the page's three buckets (decide / fix / clear). Its header carries the same
// number the ledger shows, so "2 need you" and "2 · Needs your call" are visibly the same thing.
function ClassSection({
  id,
  n,
  title,
  blurb,
  tone = "",
  empty = null,
  children,
}: {
  id: string;
  n: number | undefined;
  title: string;
  blurb: string;
  tone?: string;
  empty?: string | null;
  children?: React.ReactNode;
}) {
  return (
    <section id={id} className={`klass ${tone}`} style={{ scrollMarginTop: "5rem" }}>
      <header className="klass-head">
        <span className="klass-n tnum">{n ?? "·"}</span>
        <div>
          <h2 className="klass-title">{title}</h2>
          <p className="klass-blurb">{blurb}</p>
        </div>
      </header>
      {empty ? <p className="klass-empty">{empty}</p> : children}
    </section>
  );
}

// --- the slug-collision merge case ---------------------------------------------------------------

// Renders ONLY when there's pending work — a safe merge to apply, or a genuine collision to split.
// The receipts (applied merges, completed splits) live in Recent activity, so this card is always
// something to *do*; with no slug work it returns null and the kind reads under "all clear".
function SlugCase({
  plan,
  busy,
  onApply,
  human,
}: {
  plan: { data: CollisionPlan | null; error: string | null; loading: boolean };
  busy: Busy;
  onApply: () => void;
  human: HumanWork;
}) {
  if (!plan.data) return null; // quiet while loading; a real failure surfaces through the action note
  const { mergeable, skipped } = plan.data;
  const offer = skipped.filter((s) => s.remedy === "disambiguate");
  const manual = skipped.filter((s) => s.remedy === "manual");
  if (mergeable.length === 0 && offer.length === 0 && manual.length === 0) return null;

  const before = mergeable.length + skipped.length;
  const after = skipped.length;
  const splitN = offer.length + manual.length;
  return (
    <Case
      state="open"
      eyebrow="needs a merge"
      title="Slug collisions"
      dek={
        mergeable.length > 0
          ? `${mergeable.length} duplicate ${mergeable.length === 1 ? "page" : "pages"} would be overwritten, the original lost.`
          : `${splitN} ${splitN === 1 ? "pair shares" : "pairs share"} a page filename but aren't duplicates.`
      }
    >
      {mergeable.length > 0 && (
        <>
          <Act n="One" label="What's wrong">
            <p className="act-lede">
              These pairs each resolve to the <em>same</em> page filename — two entities, one file.
            </p>
            <ClashList pairs={mergeable.map((m) => ({ src: m.src.name, dst: m.dst.name, slug: m.dst.stem }))} />
          </Act>

          <Act n="Two" label="Why it matters">
            <p className="act-prose">
              Two entities can't own one file. Whichever the synthesizer writes second overwrites the
              first, so a page silently vanishes and every link from other pages breaks. Folding the
              duplicate into the original leaves one page holding both sets of claims — nothing lost.
            </p>
          </Act>

          <Act n="Three" label="How to fix it">
            <div className="contract">
              <div className="contract-head">
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
                <button className="action" disabled={busy !== null} onClick={onApply}>
                  {busy === "apply"
                    ? "Merging…"
                    : `Apply ${mergeable.length} merge${mergeable.length === 1 ? "" : "s"}`}
                </button>
              </div>
              <ul className="contract-ticks">
                <li>
                  {mergeable.length} duplicate {mergeable.length === 1 ? "page folds" : "pages fold"} —
                  every claim kept on the survivor
                </li>
                {skipped.length > 0 && (
                  <li>
                    the {skipped.length} that {skipped.length === 1 ? "remains is" : "remain are"}{" "}
                    genuinely different — split onto {skipped.length === 1 ? "its" : "their"} own page below
                  </li>
                )}
                <li>reversible — undo the whole batch from Recent activity</li>
              </ul>
            </div>
          </Act>
        </>
      )}
      <HumanCases offer={offer} manual={manual} human={human} />
    </Case>
  );
}

// The merge receipt, demoted from a case to a Recent-activity entry: it records what happened, it isn't
// a problem. `live` (just applied this session) animates the predicted→verified count and settles the
// rows in; a rehydrated `recent` batch is static. Either way it carries the sha and the batch Undo.
function MergeReceipt({
  live,
  recent,
  busy,
  onUndo,
}: {
  live: LiveReceipt | null;
  recent: RecentBatch | null;
  busy: Busy;
  onUndo: (sha: string) => void;
}) {
  const animate = !!live;
  const pairs = live ? live.predicted : recent?.merges.map((m) => ({ src: m.src, dst: m.dst })) ?? [];
  const appliedBySrc = live
    ? live.appliedBySrc
    : new Map((recent?.merges ?? []).map((m) => [m.src, m.dst]));
  const merged = live ? live.merged : recent?.count ?? 0;
  const predictedCount = live ? live.predicted.length : recent?.count ?? 0;
  const sha = live ? live.sha : recent?.sha ?? null;

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
    <div className="activity-entry">
      <div className={`verify ${allLanded ? "ok" : "warn"}`}>
        <span className="verify-mark">{allLanded ? "✓" : "⚠"}</span>
        <span className="verify-line">
          {animate ? (
            <>
              We predicted <span className="mono">{predictedCount}</span>.{" "}
              <strong className="tnum">{shown}</strong> merged
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
    </div>
  );
}

// --- case + act scaffolding ----------------------------------------------------------------------

function Case({
  state,
  title,
  dek,
  stamp,
  eyebrow,
  children,
}: {
  state: "open" | "resolved" | "clean";
  title: string;
  dek: string;
  stamp?: string; // overrides the state's default word (e.g. "Triage" for a read-only survey)
  eyebrow: string; // what this case needs — "needs a decision", "needs a merge" — not a category label
  children?: React.ReactNode;
}) {
  const word = stamp ?? { open: "Open", resolved: "Resolved", clean: "Clear" }[state];
  return (
    <section className={`case ${state}`}>
      <header className="case-head">
        <div>
          <div className="kicker">{eyebrow}</div>
          <h2 className="case-title">{title}</h2>
          <p className="case-dek">{dek}</p>
        </div>
        <span className={`case-stamp ${state}`}>{word}</span>
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

// The disambiguation half of the slug-collision case. A genuine collision (same slug, distinct
// entities) can't be merged — that would conflate two different things — but it can be *split*: each
// entity gets its own page. This is the pending half: the offer to split what shares a slug, plus any
// pair that still needs a human. (What's already been split lives in Recent activity, with its Undo.)
function HumanCases({
  offer,
  manual,
  human,
}: {
  offer: CollisionPlan["skipped"];
  manual: CollisionPlan["skipped"];
  human: HumanWork;
}) {
  const { busy, onDisambiguate } = human;
  if (offer.length === 0 && manual.length === 0) return null;

  return (
    <div className="remainder">
      {offer.length > 0 && (
        <div className="split-offer">
          <div className="split-offer-head">
            <div className="remainder-head mono">
              {offer.length === 1
                ? "1 shares a slug but isn't a duplicate"
                : `${offer.length} share a slug but aren't duplicates`}
            </div>
            <button className="action ghost" disabled={busy !== null} onClick={onDisambiguate}>
              {busy === "split"
                ? "Splitting…"
                : `Give each its own page${offer.length > 1 ? ` (${offer.length})` : ""}`}
            </button>
          </div>
          {offer.map((m, i) => (
            <div className="remainder-row" key={i}>
              <span className="r-from">{m.src.name}</span>
              <span className="r-arrow mono">~</span>
              <span className="r-into">{m.dst.name}</span>
              <span className="r-reason muted">only the slugifier collides them</span>
            </div>
          ))}
        </div>
      )}

      {manual.length > 0 && (
        <div className="manual-left">
          <div className="remainder-head mono">{manual.length} left for a human</div>
          {manual.map((m, i) => (
            <div className="remainder-row" key={i}>
              <span className="r-from">{m.src.name}</span>
              <span className="r-arrow mono">~</span>
              <span className="r-into">{m.dst.name}</span>
              <span className="r-reason muted">{m.reason}</span>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}

// --- duplicates: a read-only triage of the 255 near-duplicates, before any merge tool exists ------

function DuplicatesCase({
  state,
}: {
  state: { data: DuplicatePlan | null; error: string | null; loading: boolean };
}) {
  if (state.loading && !state.data)
    return <Case state="open" eyebrow="needs a decision" stamp="Triage" title="Duplicates" dek="Scoring the near-duplicates…" />;
  if (state.error || !state.data)
    return (
      <Case state="open" eyebrow="needs a decision" stamp="Triage" title="Duplicates" dek="Couldn't score the duplicates — is the API up?" />
    );
  return <DuplicatesPlan plan={state.data} />;
}

const BAND_KEYS = ["fold", "judge", "aside"] as const;

// Live triage: the two gauge cuts are draggable. Band *counts* re-bucket from `scores` (always sent);
// *samples* re-bucket from `pairs` when the API sends them, else fall back to the server's default-cut
// sample. Read-only — nothing is merged; this only tunes where the bands fall before a heal is wired up.
function DuplicatesPlan({ plan }: { plan: DuplicatePlan }) {
  const [judge, setJudge] = useState(plan.cuts.judge);
  const [fold, setFold] = useState(plan.cuts.fold);
  const dirty = judge !== plan.cuts.judge || fold !== plan.cuts.fold;
  const reset = () => {
    setJudge(plan.cuts.judge);
    setFold(plan.cuts.fold);
  };

  const bandOf = (s: number) => (s >= fold ? "fold" : s >= judge ? "judge" : "aside");
  const meta = Object.fromEntries(plan.bands.map((b) => [b.key, b])) as Record<string, DupBand>;
  const count = (key: string) => plan.scores.filter((s) => bandOf(s) === key).length;
  const sampleOf = (key: string) =>
    plan.pairs?.length
      ? plan.pairs.filter((p) => bandOf(p.score) === key).slice(0, 6)
      : meta[key]?.sample ?? [];
  const foldCount = count("fold");

  return (
    <Case
      state="open"
      eyebrow="needs a decision"
      stamp="Triage"
      title="Duplicates"
      dek={`${plan.total} pairs share a name — but the signals say most are look-alikes, not the same entity.`}
    >
      <ConfidenceGauge
        scores={plan.scores}
        judge={judge}
        fold={fold}
        defaults={plan.cuts}
        onJudge={setJudge}
        onFold={setFold}
      />
      <div className="bands">
        {BAND_KEYS.map((key) => (
          <BandRow key={key} bandKey={key} meta={meta[key]} count={count(key)} sample={sampleOf(key)} />
        ))}
      </div>
      <p className="band-foot muted">
        {dirty ? (
          <>
            Tuned to {judge.toFixed(2)} / {fold.toFixed(2)} —{" "}
            <button type="button" className="linklike" onClick={reset}>
              reset to {plan.cuts.judge.toFixed(2)} / {plan.cuts.fold.toFixed(2)}
            </button>
            . Dragging only previews the split; nothing is merged.
          </>
        ) : (
          <>
            A read-only plan — scored with the resolver's own signals, not yet merged.{" "}
            {foldCount === 0
              ? "Nothing's confident enough to fold on its own."
              : `Only ${foldCount} ${foldCount === 1 ? "pair is" : "pairs are"} confident enough to fold on ${foldCount === 1 ? "its" : "their"} own.`}{" "}
            Drag the gauge handles to tune the bands.
          </>
        )}
      </p>
    </Case>
  );
}

// The signature: every pair as a tick along a confidence axis, the two decision thresholds drawn in as
// draggable handles. The pile-up at the low end is the point — most "duplicates" are really look-alikes.
function ConfidenceGauge({
  scores,
  judge,
  fold,
  onJudge,
  onFold,
}: {
  scores: number[];
  judge: number;
  fold: number;
  defaults: { judge: number; fold: number };
  onJudge: (v: number) => void;
  onFold: (v: number) => void;
}) {
  const trackRef = useRef<HTMLDivElement>(null);
  if (scores.length === 0) return null;
  const clamp = (v: number, a: number, b: number) => Math.max(a, Math.min(b, v));
  // Clip the axis to the populated score range (+ a small margin) so the distribution fills the width
  // rather than stranding empty ends; the handle labels keep the absolute scale readable.
  const lo = Math.max(0, Math.min(...scores) - 0.04);
  const hi = Math.min(1, Math.max(...scores) + 0.04);
  const pos = (s: number) => ((clamp(s, lo, hi) - lo) / (hi - lo)) * 100;
  const zone = (a: number, b: number) => ({ left: `${pos(a)}%`, width: `${pos(b) - pos(a)}%` });
  const bandOf = (s: number) => (s >= fold ? "fold" : s >= judge ? "judge" : "aside");
  const round2 = (v: number) => Math.round(v * 100) / 100;

  // A cut stays inside the axis and never crosses its neighbour (judge below fold, by at least 0.01).
  const setCut = (which: "judge" | "fold", v: number) =>
    which === "judge" ? onJudge(clamp(v, lo, round2(fold - 0.01))) : onFold(clamp(v, round2(judge + 0.01), hi));

  const drag = (which: "judge" | "fold") => (e: React.PointerEvent) => {
    e.preventDefault();
    const at = (clientX: number) => {
      const r = trackRef.current?.getBoundingClientRect();
      if (!r || r.width === 0) return;
      setCut(which, round2(lo + clamp((clientX - r.left) / r.width, 0, 1) * (hi - lo)));
    };
    at(e.clientX);
    const move = (ev: PointerEvent) => at(ev.clientX);
    const up = () => {
      window.removeEventListener("pointermove", move);
      window.removeEventListener("pointerup", up);
    };
    window.addEventListener("pointermove", move);
    window.addEventListener("pointerup", up);
  };

  const onKey = (which: "judge" | "fold") => (e: React.KeyboardEvent) => {
    const step = e.shiftKey ? 0.05 : 0.01;
    const d = e.key === "ArrowLeft" ? -step : e.key === "ArrowRight" ? step : 0;
    if (!d) return;
    e.preventDefault();
    setCut(which, round2((which === "judge" ? judge : fold) + d));
  };

  // Inlined (not a child component) so the buttons keep their identity across renders and don't lose
  // focus mid-drag / mid-keypress.
  const handle = (which: "judge" | "fold", value: number) => (
    <button
      type="button"
      className={`gauge-handle ${which}`}
      style={{ left: `${pos(value)}%` }}
      onPointerDown={drag(which)}
      onKeyDown={onKey(which)}
      role="slider"
      aria-label={which === "judge" ? "Look-alike to judge cut" : "Judge to fold cut"}
      aria-valuemin={which === "judge" ? round2(lo) : judge}
      aria-valuemax={which === "judge" ? fold : round2(hi)}
      aria-valuenow={value}
    >
      <i className="mono">{value.toFixed(2)}</i>
    </button>
  );

  return (
    <div className="gauge">
      <div className="gauge-scale" ref={trackRef}>
        <span className="gauge-zone aside" style={zone(lo, judge)} />
        <span className="gauge-zone judge" style={zone(judge, fold)} />
        <span className="gauge-zone fold" style={zone(fold, hi)} />
        {scores.map((s, i) => (
          <span className={`gauge-tick ${bandOf(s)}`} style={{ left: `${pos(s)}%` }} key={i} />
        ))}
        {handle("judge", judge)}
        {handle("fold", fold)}
      </div>
      <div className="gauge-axis mono">
        <span>look-alike</span>
        <span>same entity</span>
      </div>
    </div>
  );
}

// Remember a disclosure's open state across re-renders and reloads (keyed in localStorage), so merging
// a pair — which re-renders the list — doesn't snap "show examples" shut.
function usePersistentOpen(key: string): [boolean, (v: boolean) => void] {
  const [open, setOpen] = useState<boolean>(() => {
    try {
      return localStorage.getItem(`maint-open:${key}`) === "1";
    } catch {
      return false;
    }
  });
  const set = (v: boolean) => {
    setOpen(v);
    try {
      localStorage.setItem(`maint-open:${key}`, v ? "1" : "0");
    } catch {
      /* private mode — fall back to in-memory only */
    }
  };
  return [open, set];
}

// Inline merge for an obvious duplicate — no trip to Compare needed. On success the button flips to
// Undo (reversing that exact fold by its wiki-commit sha), so a wrong click is one click back. Self-
// contained: it shows its own failure inline (e.g. a 401 when the admin token's required).
function MergeButton({ a, b }: { a: DupPair["a"]; b: DupPair["b"] }) {
  const [state, setState] = useState<"idle" | "busy" | "done" | "error">("idle");
  const [sha, setSha] = useState<string | null>(null);
  const [why, setWhy] = useState("");

  const merge = async () => {
    setState("busy");
    try {
      const r = await api.foldDuplicate(a.id, b.id);
      setSha(r.sha);
      setState("done");
    } catch (e) {
      const m = String(e);
      setWhy(m.includes("401") ? "needs token" : m.includes("409") ? "gone" : "failed");
      setState("error");
    }
  };
  const undo = async () => {
    if (!sha) return;
    setState("busy");
    try {
      await api.undoMerge(sha);
      setSha(null);
      setState("idle");
    } catch {
      setWhy("undo failed");
      setState("error");
    }
  };

  if (state === "done")
    return (
      <button type="button" className="band-merge done" onClick={undo}>
        ↺ undo
      </button>
    );
  if (state === "error")
    return (
      <button type="button" className="band-merge error" onClick={merge} title={why}>
        {why}
      </button>
    );
  return (
    <button type="button" className="band-merge" onClick={merge} disabled={state === "busy"}>
      {state === "busy" ? "…" : "merge"}
    </button>
  );
}

function BandRow({
  bandKey,
  meta,
  count,
  sample,
}: {
  bandKey: string;
  meta: DupBand | undefined;
  count: number;
  sample: DupBand["sample"];
}) {
  const [open, setOpen] = usePersistentOpen(`band:${bandKey}`);
  if (!meta) return null;
  return (
    <div className={`band ${bandKey}`}>
      <div className="band-head">
        <span className="band-n tnum">{count}</span>
        <div className="band-said">
          <span className="band-verb">{meta.verb}</span>
          <span className="band-gloss muted">{meta.gloss}</span>
        </div>
      </div>
      {sample.length > 0 && (
        <details className="band-ev" open={open} onToggle={(e) => setOpen(e.currentTarget.open)}>
          <summary>show examples</summary>
          <ul>
            {sample.map((p) => (
              // keyed by the pair, not the index, so a merged button stays bound to its pair when the
              // gauge re-buckets the sample.
              <li className="band-row" key={`${p.a.id}-${p.b.id}`}>
                <MergeButton a={p.a} b={p.b} />
                <Link className="band-pair" to={`/maintenance/compare/${p.a.id}/${p.b.id}`}>
                  <span className="r-from">{p.a.name}</span>
                  <span className="r-arrow mono">~</span>
                  <span className="r-into">{p.b.name}</span>
                  <span className="band-score mono">{p.score.toFixed(2)}</span>
                  <span className="band-go mono" aria-hidden="true">
                    compare →
                  </span>
                </Link>
              </li>
            ))}
          </ul>
        </details>
      )}
    </div>
  );
}

// --- the uniform issue case: any flagged kind, in the same what / why / fix / undo shape ----------

function IssueCase({ group }: { group: FindingGroup }) {
  const c = CHECK[group.kind];
  if (!c) return null;
  return (
    <Case
      state="open"
      eyebrow={c.eyebrow}
      stamp="Open"
      title={c.title}
      dek={`${group.count} ${group.count === 1 ? "item" : "items"} flagged.`}
    >
      <dl className="anatomy">
        <div className="ana-row">
          <dt>why it matters</dt>
          <dd>{c.why}</dd>
        </div>
        <div className="ana-row">
          <dt>how to fix it</dt>
          <dd>{c.fix}</dd>
        </div>
        <div className="ana-row">
          <dt>how to undo</dt>
          <dd>{c.undo}</dd>
        </div>
      </dl>
      <details className="finding-ev">
        <summary>show {group.count === 1 ? "it" : `all ${group.count}`}</summary>
        <ul>
          {group.items.slice(0, 50).map((it, i) => (
            <li key={i}>
              {it.ref &&
                (it.link ? (
                  <Link className="fg-ref fg-link" to={`/wiki/entity/${it.link}`}>
                    {it.ref} →
                  </Link>
                ) : (
                  <span className="fg-ref mono">{it.ref}</span>
                ))}{" "}
              {it.detail}
            </li>
          ))}
          {group.items.length > 50 && <li className="muted">…and {group.items.length - 50} more</li>}
        </ul>
      </details>
    </Case>
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

// --- recent activity: the undo log. Not a case — a record of what you've done, each reversible. ----

function RecentActivity({
  live,
  recent,
  splits,
  busy,
  onUndo,
  onUndoSplit,
}: {
  live: LiveReceipt | null;
  recent: RecentBatch | null;
  splits: RecentSplits | null;
  busy: Busy;
  onUndo: (sha: string) => void;
  onUndoSplit: (ids: number[]) => void;
}) {
  const hasMerges = !!(live || recent?.sha);
  const done = splits?.entities ?? [];
  return (
    <section className="activity">
      <div className="rule-head">
        <h2>Recent activity</h2>
        <span className="lighter-sub mono">reversible — undo here</span>
      </div>
      {!hasMerges && done.length === 0 ? (
        <p className="empty">Nothing on record yet. Anything you apply lands here, reversible.</p>
      ) : (
        <>
          {hasMerges && <MergeReceipt live={live} recent={recent} busy={busy} onUndo={onUndo} />}
          {done.length > 0 && <SplitReceipt done={done} busy={busy} onUndoSplit={onUndoSplit} />}
        </>
      )}
    </section>
  );
}

function SplitReceipt({
  done,
  busy,
  onUndoSplit,
}: {
  done: SplitEntity[];
  busy: Busy;
  onUndoSplit: (ids: number[]) => void;
}) {
  return (
    <div className="split-receipt">
      <div className="split-receipt-head">
        <span className="r-check ok">✓</span>
        <span className="split-msg">
          Resolved {done.length === 1 ? "a slug collision" : `${done.length} slug collisions`} — gave{" "}
          {done.length === 1 ? "an entity" : "each entity"} its own page.
        </span>
        <button className="undo" disabled={busy !== null} onClick={() => onUndoSplit(done.map((d) => d.id))}>
          {busy === "unsplit" ? "Undoing…" : "Undo"}
        </button>
      </div>
      {done.map((d) => (
        <div className="split-row" key={d.id}>
          <Link className="r-from" to={`/wiki/entity/${d.slug}`}>
            {d.name}
          </Link>
          <span className="r-reason muted">
            was colliding on <span className="mono">{d.base}.md</span>, now at
          </span>
          <span className="split-slug mono">{d.slug}.md</span>
        </div>
      ))}
    </div>
  );
}

// Surfaced only when an action 401s — the admin token the server requires. Off in single-user dev, so
// it never shows there; when DOCTALK_ADMIN_TOKEN is set it appears inline against the action that needs it.
function AdminPrompt({ onSaved }: { onSaved: () => void }) {
  const [val, setVal] = useState(getAdminToken());
  return (
    <div className="admin-prompt">
      <p className="muted at-note">
        That action needs the admin token the server set in <code>DOCTALK_ADMIN_TOKEN</code>. It's kept
        in this browser and sent only with Apply and Undo.
      </p>
      <div className="token-row">
        <input
          type="password"
          value={val}
          placeholder="admin token"
          onChange={(e) => setVal(e.target.value)}
        />
        <button
          className="action"
          onClick={() => {
            setAdminToken(val.trim());
            onSaved();
          }}
        >
          Save token
        </button>
      </div>
    </div>
  );
}
