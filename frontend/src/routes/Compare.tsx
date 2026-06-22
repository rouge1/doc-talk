import { useState } from "react";
import { Link, useParams, useSearchParams } from "react-router-dom";
import { api, sourcePath, type EvidenceSide } from "../api";
import { useFetch } from "../useFetch";

type Note = { text: string; tone: "done" | "undone" | "error" } | null;

// Two entities the resolver flagged as possible duplicates, set side by side as exhibits: the same
// surface term highlighted in each one's real source passages, so a human can read both contexts and
// judge whether they name the same thing. Read-only — the decision is the reader's; the merge tool
// (and its reversible apply) lands with the duplicates heal.

const BAND_LABEL: Record<string, string> = {
  fold: "likely the same",
  judge: "genuinely ambiguous",
  aside: "likely distinct",
};

function escapeRe(s: string): string {
  return s.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
}

// Light up every surface form of the entity in the passage. Longest-first so a fuller name
// ("BR/EDR/LE Controller") wins over a substring ("Controller"). Alphanumeric lookarounds keep a short
// alias from matching inside a longer word — "ATT" lights up in "ATT_ERROR_RSP" (an underscore is a
// boundary) but not inside "attribute" — so the passage stays readable instead of a wall of marks.
function Highlight({ text, terms }: { text: string; terms: string[] }) {
  const cleaned = [...new Set(terms.filter((t) => t.trim()))]
    .sort((a, b) => b.length - a.length)
    .map(escapeRe);
  if (cleaned.length === 0) return <>{text}</>;
  const parts = text.split(new RegExp(`(?<![A-Za-z0-9])(${cleaned.join("|")})(?![A-Za-z0-9])`, "gi"));
  return (
    <>
      {parts.map((p, i) =>
        i % 2 === 1 ? (
          <mark className="ev-hl" key={i}>
            {p}
          </mark>
        ) : (
          <span key={i}>{p}</span>
        ),
      )}
    </>
  );
}

// A 1-claim entity sitting next to a 24-claim one is almost always a fragment of it — the lopsidedness
// is the single most decisive thing on the page. Say it in the thin column instead of leaving dead space.
function fragmentNote(side: EvidenceSide, other: EvidenceSide): string | null {
  if (side.claims <= 2 && other.claims >= 5 && other.claims >= side.claims * 5) {
    const n = `${side.claims} claim${side.claims === 1 ? "" : "s"}`;
    return `${n} here, against ${other.claims} on ${other.name}. A near-empty entity beside a full one is usually a fragment of it — read the passage and see if the other side already covers it.`;
  }
  return null;
}

function Exhibit({ side, note }: { side: EvidenceSide; note: string | null }) {
  // When the entity's own name never appears in its passages, the marks are all on its aliases — which
  // can read as "nothing matched the title." Say so, and point at what's actually lit, so the reader
  // isn't hunting for a phrase the source never uses.
  const nameShown = side.passages.some((p) => p.text.toLowerCase().includes(side.name.toLowerCase()));
  return (
    <section className="exhibit">
      <header className="exhibit-head">
        <Link className="exhibit-name" to={`/wiki/entity/${side.stem}`}>
          {side.name}
        </Link>
        <div className="exhibit-meta mono">
          {side.type} · {side.sources} source{side.sources === 1 ? "" : "s"} · {side.claims} claim
          {side.claims === 1 ? "" : "s"}
        </div>
        {side.aliases.length > 0 && <div className="exhibit-aka">also: {side.aliases.join(" · ")}</div>}
      </header>

      {!nameShown && side.aliases.length > 0 && side.passages.length > 0 && (
        <p className="ev-alias-note">
          Not written by name in these passages — the source uses{" "}
          <b>{side.aliases.join(" · ")}</b>, highlighted below.
        </p>
      )}

      {side.passages.length === 0 ? (
        <p className="empty">No source passages recorded for this entity.</p>
      ) : (
        side.passages.map((p, i) => (
          <article className="ev" key={i}>
            <p className="ev-text">
              <Highlight text={p.text} terms={side.terms} />
            </p>
            <Link
              className="ev-src mono"
              to={sourcePath(
                {
                  content_hash: p.content_hash,
                  file: p.file,
                  page: p.page,
                  chapter_id: p.chapter_id,
                  chunk_id: p.chunk_id,
                },
                side.name,
              )}
            >
              {p.file ?? "source"} · p.{p.page} →
            </Link>
          </article>
        ))
      )}

      {note && (
        <p className="ev-thin">
          <b>Far thinner than its match.</b> {note}
        </p>
      )}
    </section>
  );
}

export default function Compare() {
  const { a = "", b = "" } = useParams();
  // Which list sent us here, so "Back to the list" returns to the right section (and the right verb is
  // used to fold). An unresolved pair came from /maintenance#unresolved with the unresolved entity as
  // `a`; everything else is a Duplicates pair.
  const [params] = useSearchParams();
  const from = params.get("from") === "unresolved" ? "unresolved" : "duplicates";
  const backTo = `/maintenance#${from}`;
  const backLabel = from === "unresolved" ? "Unresolved entities" : "Duplicates";
  const { data, error, loading } = useFetch(
    () => api.comparePair(Number(a), Number(b)),
    `compare:${a}:${b}`,
  );
  const [busy, setBusy] = useState(false);
  const [note, setNote] = useState<Note>(null);
  const [folded, setFolded] = useState<{ sha: string | null } | null>(null);

  const fold = async () => {
    if (!data) return;
    setBusy(true);
    setNote(null);
    try {
      // From the unresolved list the fold is directional (the provisional `a` always folds into its
      // candidate `b`); a Duplicates pair lets the richer side win regardless of order.
      const r =
        from === "unresolved"
          ? await api.mergeUnresolved(data.a.id, data.b.id)
          : await api.foldDuplicate(data.a.id, data.b.id);
      setFolded({ sha: r.sha });
      setNote({ text: `Folded ${r.folded} into ${r.into}.`, tone: "done" });
    } catch (e) {
      const m = String(e);
      setNote({
        text: m.includes("401")
          ? "This needs the admin token — set it on the Maintain page, then come back."
          : m.includes("409")
            ? "This pair changed since you opened it — head back to the list."
            : `Couldn't fold: ${e}`,
        tone: "error",
      });
    } finally {
      setBusy(false);
    }
  };

  const undo = async () => {
    if (!folded?.sha) return;
    setBusy(true);
    try {
      await api.undoMerge(folded.sha);
      setFolded(null);
      setNote({ text: "Unfolded — both entities are back.", tone: "undone" });
    } catch (e) {
      setNote({ text: `Couldn't undo: ${e}`, tone: "error" });
    } finally {
      setBusy(false);
    }
  };

  if (loading) return <div className="loading">Pulling the passages…</div>;
  if (error || !data) return <div className="empty">Couldn't load this comparison.</div>;

  return (
    <div className="rise compare">
      <div className="compare-topbar">
        <div className="crumbs">
          <Link to="/maintenance">Maintain</Link> &nbsp;/&nbsp;{" "}
          <Link to={backTo}>{backLabel}</Link> &nbsp;/&nbsp; compare
        </div>
        <Link className="back-pill" to={backTo}>
          ← Back to the list
        </Link>
      </div>

      <section className="compare-hero">
        <p className="compare-ask">Same entity?</p>
        <h1 className="compare-q">
          <span>{data.a.name}</span>
          <span className="compare-tilde" aria-hidden="true">
            ~
          </span>
          <span>{data.b.name}</span>
        </h1>
        <div className="compare-verdict">
          <span className={`verdict-band ${data.band}`}>{BAND_LABEL[data.band] ?? data.band}</span>
          <span className="verdict-meta mono">
            the resolver scores this {data.score.toFixed(2)} — lexical {data.signals.lexical} · embed{" "}
            {data.signals.embed} · co-mention {data.signals.comention}
          </span>
        </div>
        <p className="compare-lede">
          The same word in two contexts. Read both columns — if it means the same thing in each, they're
          one entity.
        </p>
      </section>

      <div className="compare-cols">
        <Exhibit side={data.a} note={fragmentNote(data.a, data.b)} />
        <Exhibit side={data.b} note={fragmentNote(data.b, data.a)} />
      </div>

      <div className="verdict-actions">
        {folded ? (
          <span className="verdict-hint muted">Folded. Undo if that wasn't right.</span>
        ) : (
          <>
            <button type="button" className="fold-btn" onClick={fold} disabled={busy}>
              {busy ? "Folding…" : "Same — fold together"}
            </button>
            <span className="verdict-hint muted">
              {from === "unresolved"
                ? "Folds this unresolved entity into its match, keeping every claim. Reversible."
                : "Folds the thinner page into the richer one, keeping every claim. Reversible."}
            </span>
          </>
        )}
      </div>

      {note && (
        <div className={`compare-note ${note.tone}`} role="status">
          <span className="mk mono" aria-hidden="true">
            {note.tone === "error" ? "!" : note.tone === "undone" ? "↺" : "✓"}
          </span>
          <span>{note.text}</span>
          {folded && note.tone === "done" && (
            <button type="button" className="linklike" onClick={undo} disabled={busy}>
              Undo
            </button>
          )}
        </div>
      )}

      <div className="compare-foot">
        <Link className="back-pill" to={backTo}>
          ← Back to the list
        </Link>
      </div>
    </div>
  );
}
