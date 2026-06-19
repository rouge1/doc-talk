import { Link, useNavigate, useParams } from "react-router-dom";
import { api } from "../api";
import { useFetch } from "../useFetch";

// A source document's synthesis profile — the top rung of the wiki. The authored lead is
// server-rendered HTML (entity wikilinks); Contents links into the document reader, Key entities
// into their folios.
export default function Source() {
  const { stem = "" } = useParams();
  const navigate = useNavigate();
  const { data, error, loading } = useFetch(() => api.source(stem), `source:${stem}`);

  if (loading) return <div className="loading">Retrieving the source…</div>;
  if (error || !data) return <div className="empty">Source not found in the archive.</div>;

  // The lead's wikilinks render as Jinja /wiki/page/<stem>; in the SPA those are entity folios.
  const leadHtml = data.lead.replace(/href="\/wiki\/page\//g, 'href="/wiki/entity/');
  const onLeadClick = (e: React.MouseEvent) => {
    const a = (e.target as HTMLElement).closest("a.wikilink") as HTMLAnchorElement | null;
    if (a && a.getAttribute("href")?.startsWith("/wiki/")) {
      e.preventDefault();
      navigate(a.getAttribute("href")!);
    }
  };

  const meta = [
    data.size,
    `${data.chapters} chapter${data.chapters !== 1 ? "s" : ""}`,
    `${data.entities} entities`,
    `${data.claims} claims`,
    data.ingested ? `ingested ${data.ingested}` : null,
  ].filter(Boolean).join(" · ");

  return (
    <div className="rise">
      <div className="crumbs"><Link to="/">Library</Link> &nbsp;/&nbsp; source</div>

      <header className="folio">
        <span className="badge">source · {data.format}</span>
        <h1>{data.title}</h1>
        <div className="alias">{meta}</div>
      </header>

      {data.lead && (
        <div className="prose" style={{ marginTop: "1.4rem" }} onClick={onLeadClick}
             dangerouslySetInnerHTML={{ __html: leadHtml }} />
      )}

      {data.contents.length > 0 && (
        <>
          <div className="rule-head">
            <h2>Contents</h2>
            <span className="count tnum">{data.contents.length}</span>
          </div>
          <div className="catalog">
            {data.contents.map((c, i) => (
              <Link key={c.chapter_id} className="entry" to={`/doc/${data.hash}/chapter/${c.chapter_id}`}>
                <span className="callno tnum">{String(i + 1).padStart(2, "0")}</span>
                <span><div className="title">{c.title}</div></span>
                <span className="meta">{c.entities} entities</span>
              </Link>
            ))}
          </div>
        </>
      )}

      {data.key_entities.length > 0 && (
        <>
          <div className="rule-head"><h2>Key entities</h2></div>
          <div className="related">
            {data.key_entities.map((e) =>
              e.stem ? (
                <Link className="chip" key={e.name} to={`/wiki/entity/${e.stem}`}>{e.name}</Link>
              ) : (
                <span className="chip" key={e.name} style={{ opacity: 0.55 }}>{e.name}</span>
              ),
            )}
          </div>
        </>
      )}
    </div>
  );
}
