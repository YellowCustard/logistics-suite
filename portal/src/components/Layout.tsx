import { NavLink, Outlet } from "react-router";

import { useAuth, useIsClientAdmin } from "../auth";

// Sections appear here as they are built (see the portal plan in docs).
const SECTIONS = [
  { to: "/", label: "Home", end: true },
  { to: "/orders", label: "Orders" },
  { to: "/inbound", label: "Inbound" },
  { to: "/stock", label: "Stock" },
  { to: "/shipments", label: "Tracking" },
  { to: "/items", label: "Items" },
  { to: "/documents", label: "Documents" },
];

export function Layout() {
  const { session, signOut } = useAuth();
  const sections = useIsClientAdmin() ? [...SECTIONS, { to: "/settings", label: "Settings" }] : SECTIONS;
  return (
    <>
      <header className="bar">
        <NavLink to="/" className="brand">
          <img src="/static/brand/mark-simple.svg" alt="" width={24} height={24} />
          <span className="title">Portal</span>
        </NavLink>
        <nav aria-label="Sections">
          {sections.map((s) => (
            <NavLink key={s.to} to={s.to} end={s.end}>
              {s.label}
            </NavLink>
          ))}
        </nav>
        <div className="who">
          <span>{session?.email}</span>
          <button type="button" className="ghost" onClick={() => void signOut()}>
            Sign out
          </button>
        </div>
      </header>
      <main>
        <Outlet />
      </main>
    </>
  );
}
