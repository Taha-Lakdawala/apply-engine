import { NavLink, Route, Routes } from "react-router-dom";
import Dashboard from "./pages/Dashboard";
import Applications from "./pages/Applications";
import ApplicationDetail from "./pages/ApplicationDetail";
import Profile from "./pages/Profile";
import Questions from "./pages/Questions";

export default function App() {
  return (
    <div className="layout">
      <aside className="sidebar">
        <div className="brand">
          <div className="brand-mark">A</div>
          <div>
            <div className="brand-name">apply-engine</div>
            <div className="brand-sub">dashboard</div>
          </div>
        </div>
        <nav>
          <NavItem to="/" end>Overview</NavItem>
          <NavItem to="/applications">Applications</NavItem>
          <NavItem to="/questions">Questions</NavItem>
          <NavItem to="/profile">Profile</NavItem>
        </nav>
      </aside>
      <main className="content">
        <Routes>
          <Route path="/" element={<Dashboard />} />
          <Route path="/applications" element={<Applications />} />
          <Route path="/applications/:id" element={<ApplicationDetail />} />
          <Route path="/questions" element={<Questions />} />
          <Route path="/profile" element={<Profile />} />
        </Routes>
      </main>
    </div>
  );
}

function NavItem({ to, end, children }: { to: string; end?: boolean; children: React.ReactNode }) {
  return (
    <NavLink to={to} end={end} className={({ isActive }) => "nav-item" + (isActive ? " active" : "")}>
      {children}
    </NavLink>
  );
}
