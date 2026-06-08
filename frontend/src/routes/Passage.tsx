import { Navigate, useParams } from "react-router-dom";
import { api } from "../api";
import { useFetch } from "../useFetch";

// Resolve a chunk to the page that holds it in the rendered (converted) document, then hand off
// to the page viewer. Keeps page nav working from a real page number rather than re-locating.
export default function Passage() {
  const { hash = "", chunk = "" } = useParams();
  const { data, error, loading } = useFetch(
    () => api.find(hash, Number(chunk)),
    `find:${hash}:${chunk}`,
  );

  if (loading) return <div className="loading">Rendering the original document…</div>;
  if (error || !data) return <div className="empty">Couldn't locate this passage.</div>;
  return <Navigate to={`/doc/${hash}/page/${data.page}?focus=${chunk}`} replace />;
}
