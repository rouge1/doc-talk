import { useEffect, useState } from "react";
import { useSearchParams } from "react-router-dom";
import { api, type Gallery as GalleryData } from "../api";
import { getCached, setCached } from "../cache";

const NS = "gallery";

export default function Gallery() {
  const [params, setParams] = useSearchParams();
  const q = params.get("q") ?? "";
  const fmt = params.get("fmt") ?? "";
  const minKb = params.get("min_kb") ?? "";
  const [draft, setDraft] = useState(q);
  const [draftFmt, setDraftFmt] = useState(fmt);
  const [draftKb, setDraftKb] = useState(minKb);
  const [data, setData] = useState<GalleryData | null>(null);
  const [error, setError] = useState(false);

  // Cache-first, keyed on the filter combo — returning to the same filters (or reloading) restores
  // instantly instead of re-running CLIP; the empty/all-images view is cached too.
  const cacheKey = `${q}:${fmt}:${minKb}`;
  useEffect(() => {
    setError(false);
    const cached = getCached<GalleryData>(NS, cacheKey);
    if (cached) {
      setData(cached);
      return;
    }
    setData(null);
    let alive = true;
    api
      .gallery(q, fmt, minKb)
      .then((d) => {
        if (!alive) return;
        setData(d);
        setCached(NS, cacheKey, d);
      })
      .catch(() => alive && setError(true));
    return () => {
      alive = false;
    };
  }, [cacheKey, q, fmt, minKb]);

  useEffect(() => { // keep the inputs in sync with the URL filters
    setDraft(q);
    setDraftFmt(fmt);
    setDraftKb(minKb);
  }, [q, fmt, minKb]);

  const loading = !data && !error;

  const submit = (e: React.FormEvent) => {
    e.preventDefault();
    const next: Record<string, string> = {};
    if (draft.trim()) next.q = draft.trim();
    if (draftFmt.trim()) next.fmt = draftFmt.trim();
    if (draftKb.trim()) next.min_kb = draftKb.trim();
    setParams(next);
  };

  return (
    <div className="rise">
      <section className="hero compact">
        <div className="kicker">CLIP text→image · metadata filters</div>
        <h1 className="display">The plate room</h1>
      </section>

      <form className="filterbar" onSubmit={submit}>
        <input className="grow" value={draft} onChange={(e) => setDraft(e.target.value)}
               placeholder="describe an image (e.g. a cat in headphones)…" />
        <input value={draftFmt} onChange={(e) => setDraftFmt(e.target.value)}
               placeholder="format" style={{ width: "5.5rem" }} />
        <input value={draftKb} onChange={(e) => setDraftKb(e.target.value)} inputMode="numeric"
               placeholder="min KB" style={{ width: "5.5rem" }} />
        <button type="submit">Find</button>
      </form>

      {q && <div className="muted gal-note">ranked by visual relevance to “{q}”</div>}
      {loading && <div className="loading">Developing the plates…</div>}
      {error && <div className="empty">Couldn't load the gallery.</div>}
      {data && data.items.length === 0 && !loading && (
        <div className="empty">No images match. Clear the filters, or ingest some.</div>
      )}

      <div className="plate-grid">
        {data?.items.map((it, i) => (
          <figure key={it.file_id} className="plate-card rise" style={{ animationDelay: `${i * 35}ms` }}>
            <div className="plate-img">
              <img src={it.image} alt={it.name} loading="lazy" />
              {it.score != null && <span className="plate-score mono tnum">{it.score.toFixed(2)}</span>}
              {it.dups > 0 && <span className="plate-dups mono">+{it.dups} similar</span>}
            </div>
            <figcaption>
              <div className="plate-name">{it.name}</div>
              <div className="plate-meta mono">
                {it.fmt} · {it.kb}KB{it.when ? ` · ${it.when}` : ""}{it.geo ? ` · ${it.geo}` : ""}
              </div>
              {it.desc && <div className="plate-desc">{it.desc.slice(0, 120)}{it.desc.length > 120 ? "…" : ""}</div>}
            </figcaption>
          </figure>
        ))}
      </div>
    </div>
  );
}
