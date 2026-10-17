import { NavLink, Outlet } from "react-router-dom";
import { api } from "../lib/api";
import { usePolling } from "../lib/usePolling";

export function Layout() {
  const health = usePolling(api.health, 10_000);
  const ok = health.data?.status === "ok";
  const label = health.error
    ? "backend unreachable"
    : health.data
      ? ok
        ? `backend ok · worker ${health.data.worker.worker_id ?? "?"}`
        : `degraded: db ${health.data.database}, worker ${health.data.worker.running ? "up" : "down"}`
      : "checking…";

  return (
    <>
      <header className="topbar">
        <div className="topbar-inner">
          <span className="brand">Test Orchestration Platform</span>
          <nav>
            <NavLink to="/" end>
              Runs
            </NavLink>
            <NavLink to="/runs/new">New run</NavLink>
          </nav>
          <span className="health" title={label}>
            <span className={`health-dot ${health.error ? "bad" : ok ? "ok" : ""}`} />
            {label}
          </span>
        </div>
      </header>
      <main>
        <Outlet />
      </main>
    </>
  );
}
