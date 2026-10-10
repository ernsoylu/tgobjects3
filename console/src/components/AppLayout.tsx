import { NavLink, Outlet } from "react-router-dom";

import { useAuth } from "../auth/context";

/** Navigation entries for the console shell, in display order. */
const NAV_ITEMS: ReadonlyArray<{ to: string; label: string }> = [
  { to: "/buckets", label: "Buckets" },
  { to: "/objects", label: "Objects" },
  { to: "/jobs", label: "Jobs" },
  { to: "/recovery", label: "Recovery" },
  { to: "/settings", label: "Settings" },
];

/**
 * Persistent app layout: a sidebar of primary navigation links plus the routed
 * screen rendered through `<Outlet />`.
 *
 * The sidebar also carries a logout control (admin-dashboard Requirement 1.7):
 * it calls `signOut("logout")`, which clears the in-memory Basic credential and
 * CSRF token and flips the session to `unauthenticated`. The `RequireAuth` guard
 * then routes the now-unauthenticated operator back to `/login`, so this control
 * owns no navigation itself.
 */
export function AppLayout(): JSX.Element {
  const { signOut } = useAuth();

  return (
    <div className="app-shell">
      <aside className="app-sidebar">
        <div className="app-brand">tgobjects3</div>
        <nav className="app-nav">
          {NAV_ITEMS.map((item) => (
            <NavLink
              key={item.to}
              to={item.to}
              className={({ isActive }) =>
                isActive ? "app-nav-link app-nav-link--active" : "app-nav-link"
              }
            >
              {item.label}
            </NavLink>
          ))}
        </nav>
        <div className="app-sidebar-footer">
          <button type="button" className="btn app-logout" onClick={() => signOut("logout")}>
            Log out
          </button>
        </div>
      </aside>
      <main className="app-main">
        <Outlet />
      </main>
    </div>
  );
}
