import { NavLink, Route, Routes } from "react-router-dom";
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
        </nav>
      </div>
    </header>
  );
}

export default function App() {
  return (
    <>
      <Masthead />
      <main>
        <Routes>
          <Route path="/" element={<Library />} />
          <Route path="/search" element={<Search />} />
          <Route path="/chat" element={<Chat />} />
          <Route path="/gallery" element={<Gallery />} />
          <Route path="/jobs" element={<Jobs />} />
          <Route path="/wiki" element={<Wiki />} />
          <Route path="/wiki/source/:stem" element={<Source />} />
          <Route path="/wiki/entity/:stem" element={<Entity />} />
          <Route path="/wiki/query/:stem" element={<Query />} />
          <Route path="/doc/:hash" element={<Doc />} />
          <Route path="/doc/:hash/chapter/:chapterId" element={<Reader />} />
          <Route path="/doc/:hash/page/:page" element={<PageView />} />
          <Route path="/doc/:hash/passage/:chunk" element={<Passage />} />
        </Routes>
      </main>
    </>
  );
}
