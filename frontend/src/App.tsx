import { useEffect, useLayoutEffect, useRef } from "react";
import { NavLink, Route, Routes, useLocation, useNavigationType } from "react-router-dom";
import Library from "./routes/Library";
import Wiki from "./routes/Wiki";
import Entity from "./routes/Entity";
import Source from "./routes/Source";
import Query from "./routes/Query";
import Search from "./routes/Search";
import Chat from "./routes/Chat";
import Gallery from "./routes/Gallery";
import Jobs from "./routes/Jobs";
import Doc from "./routes/Doc";
import Reader from "./routes/Reader";
import PageView from "./routes/PageView";
import Passage from "./routes/Passage";
import Maintenance from "./routes/Maintenance";
import Compare from "./routes/Compare";
import NotFound from "./routes/NotFound";

// Remember scroll position per history entry. On a fresh navigation (PUSH/REPLACE) we land at the top;
// on back/forward (POP) we restore where the user was — which works because useFetch renders cached
// pages at full height immediately, so there's something to scroll back to. A #hash navigation is left
// alone (the target route scrolls itself to the anchor).
function ScrollManager() {
  const location = useLocation();
  const navType = useNavigationType();
  const positions = useRef<Map<string, number>>(new Map());

  useEffect(() => {
    const key = location.key;
    const onScroll = () => positions.current.set(key, window.scrollY);
    window.addEventListener("scroll", onScroll, { passive: true });
    return () => window.removeEventListener("scroll", onScroll);
  }, [location.key]);

  useLayoutEffect(() => {
    if (navType === "POP") {
      const y = positions.current.get(location.key);
      if (y != null) {
        window.scrollTo(0, y);
        return;
      }
    }
    if (!location.hash) window.scrollTo(0, 0);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [location.key]);

  return null;
}

function Masthead() {
  return (
    <header className="masthead">
      <div className="masthead-inner">
        <span className="brand">
          Doctalk<span className="dot">.</span>
        </span>
        <nav>
          <NavLink to="/" end>Library</NavLink>
          <NavLink to="/search">Search</NavLink>
          <NavLink to="/chat">Ask</NavLink>
          <NavLink to="/gallery">Gallery</NavLink>
          <NavLink to="/wiki">Wiki</NavLink>
          <NavLink to="/jobs">Ingest</NavLink>
          <NavLink to="/maintenance">Maintain</NavLink>
        </nav>
      </div>
    </header>
  );
}

export default function App() {
  return (
    <>
      <ScrollManager />
      <Masthead />
      <main>
        <Routes>
          <Route path="/" element={<Library />} />
          <Route path="/search" element={<Search />} />
          <Route path="/chat" element={<Chat />} />
          <Route path="/gallery" element={<Gallery />} />
          <Route path="/jobs" element={<Jobs />} />
          <Route path="/wiki" element={<Wiki />} />
          <Route path="/maintenance" element={<Maintenance />} />
          <Route path="/maintenance/compare/:a/:b" element={<Compare />} />
          <Route path="/wiki/source/:stem" element={<Source />} />
          <Route path="/wiki/entity/:stem" element={<Entity />} />
          <Route path="/wiki/query/:stem" element={<Query />} />
          <Route path="/doc/:hash" element={<Doc />} />
          <Route path="/doc/:hash/chapter/:chapterId" element={<Reader />} />
          <Route path="/doc/:hash/page/:page" element={<PageView />} />
          <Route path="/doc/:hash/passage/:chunk" element={<Passage />} />
          <Route path="*" element={<NotFound />} />
        </Routes>
      </main>
    </>
  );
}
