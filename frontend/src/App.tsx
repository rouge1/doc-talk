import { NavLink, Route, Routes } from "react-router-dom";
import Library from "./routes/Library";
import Wiki from "./routes/Wiki";
import Entity from "./routes/Entity";
import Query from "./routes/Query";

function Masthead() {
  return (
    <header className="masthead">
      <div className="masthead-inner">
        <span className="brand">
          Doctalk<span className="dot">.</span>
        </span>
        <nav>
          <NavLink to="/" end>Library</NavLink>
          <NavLink to="/wiki">Wiki</NavLink>
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
          <Route path="/wiki" element={<Wiki />} />
          <Route path="/wiki/entity/:stem" element={<Entity />} />
          <Route path="/wiki/query/:stem" element={<Query />} />
        </Routes>
      </main>
    </>
  );
}
