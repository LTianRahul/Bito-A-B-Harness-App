import { NavLink, Outlet } from "react-router-dom";

// Four top-level tabs — the whole app. Keeps the surface small and obvious.
const TABS = [
  { to: "/setup", label: "Setup", hint: "Detect tools & connect Bito" },
  { to: "/prompts", label: "Prompts", hint: "The tasks to test" },
  { to: "/run", label: "Run", hint: "Run an A/B test" },
  { to: "/results", label: "Results", hint: "Scores, leaderboard & report" },
];

export default function Layout() {
  return (
    <div className="shell">
      <header className="appbar">
        <div className="brand">
          <div className="brand-mark">A/B</div>
          <div>
            <div className="brand-name">A/B Testing</div>
            <div className="brand-sub">Bito AI Architect benchmark</div>
          </div>
        </div>
        <nav className="tabs">
          {TABS.map((t) => (
            <NavLink key={t.to} to={t.to} className={({ isActive }) => `tab${isActive ? " active" : ""}`} title={t.hint}>
              {t.label}
            </NavLink>
          ))}
        </nav>
      </header>
      <main className="content">
        <Outlet />
      </main>
    </div>
  );
}
