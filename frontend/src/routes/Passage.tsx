import { Navigate, useParams, useSearchParams } from "react-router-dom";
import { api } from "../api";
import { useFetch } from "../useFetch";

// Resolve a chunk to the page that holds it in the rendered (converted) document, then hand off to
// the page viewer. Keeps page nav working from a real page number rather than re-locating. With
// ?nohl (table-of-contents browsing) it opens the page plainly, without a highlight.
export default function Passage() {
  const { hash = "", chunk = "" } = useParams();
  const [params] = useSearchParams();
  const noHighlight = params.has("nohl");
  const hl = params.get("q") ?? ""; // a search query to highlight (vs ?focus for the whole chunk)
  const { data, error, loading } = useFetch(
    () => api.find(hash, Number(chunk)),
    `find:${hash}:${chunk}`,
  );

  if (loading) return <div className="loading">Rendering the original document…</div>;
  if (error || !data) return <div className="empty">Couldn't locate this passage.</div>;
  const qs = noHighlight ? "" : hl ? `?q=${encodeURIComponent(hl)}` : `?focus=${chunk}`;
  return <Navigate to={`/doc/${hash}/page/${data.page}${qs}`} replace />;
}
