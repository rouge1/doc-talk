import { useRef } from "react";
import { Link, useNavigate, useParams } from "react-router-dom";
import { api } from "../api";
import { useFetch } from "../useFetch";

export default function Query() {
  const { stem = "" } = useParams();
  const navigate = useNavigate();
  const ref = useRef<HTMLDivElement>(null);
  const { data, error, loading } = useFetch(() => api.query(stem), `query:${stem}`);

  if (loading) return <div className="loading">Retrieving inquiry…</div>;
  if (error || !data) return <div className="empty">Query not found in the archive.</div>;

  // The server renders wikilinks to the Jinja route /wiki/page/<stem>; in the SPA those targets
  // are entity folios. Rewrite the hrefs and intercept clicks for client-side routing.
  const html = data.html.replace(/href="\/wiki\/page\//g, 'href="/wiki/entity/');
  const onClick = (e: React.MouseEvent) => {
    const a = (e.target as HTMLElement).closest("a.wikilink") as HTMLAnchorElement | null;
    if (a && a.getAttribute("href")?.startsWith("/wiki/")) {
      e.preventDefault();
      navigate(a.getAttribute("href")!);
    }
  };

  return (
    <div className="rise">
      <div className="crumbs"><Link to="/wiki">Wiki</Link> &nbsp;/&nbsp; inquiry</div>
      <article className="prose" ref={ref} onClick={onClick}
        dangerouslySetInnerHTML={{ __html: html }} />
    </div>
  );
}
