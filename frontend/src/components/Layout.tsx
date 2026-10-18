import { NavLink, Outlet } from "react-router-dom";
import { api } from "../lib/api";
import { usePolling } from "../lib/usePolling";

export function Layout() {
  const health = usePolling(api.health, 10_000);
  const ok = health.data?.status === "ok";
  const workers = health.data?.workers ?? [];
  const label = health.error
    ? "backend unreachable"
    : health.data
      ? ok
        ? `backend ok · ${workers.length} worker${workers.length === 1 ? "" : "s"} alive`
        : health.data.database === "ok"
          ? "degraded: no workers alive"
          : `degraded: db ${health.data.database}`
      : "checking…";
  const title =
    workers.map((w) => `${w.worker_id}: ${w.active_runs}/${w.concurrency} runs`).join("\n") || label;

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
          <span className="health" title={title}>
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
