import { NavLink, Outlet } from "react-router-dom";

export function Layout({ sourceLabel }: { sourceLabel: string }) {
  return (
    <>
      <header className="topbar">
        <div className="topbar-inner">
          <NavLink to="/" className="brand">
            <span className="spark">✦</span> OpenShard
          </NavLink>
          <nav>
            <NavLink to="/" end>
              Recent work
            </NavLink>
          </nav>
          <span className="spacer" />
          <span className="source-chip" title="Where this dashboard is reading receipts from">
            {sourceLabel}
          </span>
        </div>
      </header>
      <main>
        <Outlet />
      </main>
    </>
  );
}
