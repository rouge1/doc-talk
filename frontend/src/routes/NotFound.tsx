import { Link, useLocation } from "react-router-dom";

// The fallback for any unmatched URL. Without it React Router renders an empty <main>, which reads as
// a broken page; this names what happened (in the archive's voice) and points back to a real shelf.
export default function NotFound() {
  const { pathname } = useLocation();
  return (
    <div className="rise">
      <section className="hero compact">
        <div className="kicker">Error 404 · no such record</div>
        <h1 className="display">This drawer is empty.</h1>
        <p>
          Nothing is filed at <span className="mono">{pathname}</span>. The page may have been moved or
          renamed — or it was never shelved here at all.
        </p>
      </section>
      <div className="empty">
        <Link className="link-btn" to="/">Return to the Library →</Link>
      </div>
    </div>
  );
}
