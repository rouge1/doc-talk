import { useRef } from "react";
import { Link, useParams, useSearchParams } from "react-router-dom";
import { api } from "../api";
import { useFetch } from "../useFetch";

// Keep the focused section's highlight across page nav (it follows a span onto the next page).
const pageHref = (hash: string, page: number, focus: number | null) =>
  `/doc/${hash}/page/${page}${focus ? `?focus=${focus}` : ""}`;

export default function PageView() {
  const { hash = "", page = "" } = useParams();
  const [params] = useSearchParams();
  const focus = Number(params.get("focus")) || null;
  const p = Number(page);
  const firstHl = useRef<HTMLSpanElement>(null);
  const { data, error, loading } = useFetch(
    () => api.page(hash, p, focus),
    `page:${hash}:${p}:${focus}`,
  );

  // Once the rasterized page has laid out, bring the first highlight into view.
  const onImgLoad = () =>
    firstHl.current?.scrollIntoView({ behavior: "smooth", block: "center" });

  if (loading) return <div className="loading">Rasterizing the original page…</div>;
  if (error || !data) return <div className="empty">Couldn't render this page.</div>;

  return (
    <div className="rise">
      <div className="crumbs">
        <Link to={`/doc/${hash}`}>{data.doc_name}</Link>
        &nbsp;/&nbsp; page <span className="tnum">{data.page}</span> of {data.page_count}
        {data.rects.length > 0 && <span className="hl-note"> · {data.rects.length} matches highlighted</span>}
      </div>

      <div className="pageframe">
        <img className="pageimg" src={data.image} alt={`page ${data.page}`} onLoad={onImgLoad} />
        {data.rects.map((r, i) => (
          <span key={i} ref={i === 0 ? firstHl : undefined} className="hl"
                style={{ left: `${r.x * 100}%`, top: `${r.y * 100}%`,
                         width: `${r.w * 100}%`, height: `${r.h * 100}%` }} />
        ))}
      </div>

      <nav className="leaf-nav mono">
        {data.page > 1
          ? <Link to={pageHref(hash, data.page - 1, focus)}>← p.{data.page - 1}</Link>
          : <span />}
        {data.page < data.page_count
          ? <Link to={pageHref(hash, data.page + 1, focus)}>p.{data.page + 1} →</Link>
          : <span />}
      </nav>
    </div>
  );
}
