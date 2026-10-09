import { NavLink, Outlet } from "react-router-dom";

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
 * screen rendered through `<Outlet />`. The individual screens land in task 44.
 */
export function AppLayout(): JSX.Element {
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
      </aside>
      <main className="app-main">
        <Outlet />
      </main>
    </div>
  );
}
