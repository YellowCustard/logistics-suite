import { NavLink, Outlet } from "react-router";

import { useIsClientAdmin } from "../auth";

export function Settings() {
  const admin = useIsClientAdmin();
  return (
    <>
      <h1>Settings</h1>
      {admin ? (
        <>
          <nav className="subnav" aria-label="Settings">
            <NavLink to="/settings/users">Users</NavLink>
            <NavLink to="/settings/api">API credentials</NavLink>
            <NavLink to="/settings/webhooks">Webhooks</NavLink>
          </nav>
          <Outlet />
        </>
      ) : (
        <p className="muted">Your account administrator manages users, API credentials and webhooks.</p>
      )}
    </>
  );
}
