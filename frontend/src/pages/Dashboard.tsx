import { useCallback, useEffect, useState } from "react";
import { Link, useNavigate } from "react-router-dom";
import { RunStateBadge } from "../components/StateBadge";
import { api, RUN_STATES, type RunState, type RunSummary } from "../lib/api";
import { elapsedSince, formatDuration, formatTime } from "../lib/format";
import { usePolling } from "../lib/usePolling";

export function Dashboard() {
  const navigate = useNavigate();
  const [environment, setEnvironment] = useState("");
  const [state, setState] = useState<RunState | "">("");
  const load = useCallback(() => api.runs({ environment, state, limit: 50 }), [environment, state]);
  const runs = usePolling(load, 1000);
  const [now, setNow] = useState(() => Date.now());

  useEffect(() => {
    const id = window.setInterval(() => setNow(Date.now()), 1000);
    return () => window.clearInterval(id);
  }, []);

  return (
    <>
      <div className="page-header">
        <h1>Runs</h1>
        <Link to="/runs/new" className="button primary">
          + Create new run
        </Link>
      </div>

      <div className="toolbar">
        <label>
          Environment
          <select value={environment} onChange={(e) => setEnvironment(e.target.value)}>
            <option value="">All</option>
            <option value="test">test</option>
            <option value="stage">stage</option>
          </select>
        </label>
        <label>
          Status
          <select value={state} onChange={(e) => setState(e.target.value as RunState | "")}>
            <option value="">All</option>
            {RUN_STATES.map((s) => (
              <option key={s} value={s}>
                {s}
              </option>
            ))}
          </select>
        </label>
        {runs.data ? (
          <span className="muted small">
            {runs.data.total} run{runs.data.total === 1 ? "" : "s"} · refreshes every second
          </span>
        ) : null}
      </div>

      {runs.error ? <div className="error-box">Could not load runs: {runs.error}</div> : null}

      <div className="card" style={{ padding: 0 }}>
        {runs.loading && !runs.data ? (
          <div className="empty">Loading…</div>
        ) : runs.data && runs.data.items.length === 0 ? (
          <div className="empty">
            No runs yet. <Link to="/runs/new">Create the first one.</Link>
          </div>
        ) : (
          <table>
            <thead>
              <tr>
                <th>Run</th>
                <th>Env</th>
                <th>Status</th>
                <th>Tests</th>
                <th>Result</th>
                <th>Started</th>
                <th>Finished</th>
                <th className="num">Duration</th>
              </tr>
            </thead>
            <tbody>
              {runs.data?.items.map((run) => (
                <RunRow key={run.id} run={run} now={now} onOpen={() => navigate(`/runs/${run.id}`)} />
              ))}
            </tbody>
          </table>
        )}
      </div>
    </>
  );
}

function RunRow({ run, now, onOpen }: { run: RunSummary; now: number; onOpen: () => void }) {
  const counts = run.test_counts;
  const executed = counts.passed + counts.failed + counts.timed_out;
  return (
    <tr className="clickable" onClick={onOpen}>
      <td>
        <Link to={`/runs/${run.id}`} className="mono" onClick={(e) => e.stopPropagation()}>
          {run.label}
        </Link>
      </td>
      <td className="mono">{run.environment}</td>
      <td>
        <RunStateBadge state={run.state} live />
        {!run.is_terminal && run.current_step ? (
          <div className="small muted">{run.current_step}</div>
        ) : null}
      </td>
      <td>
        <span className="mono">
          {counts.passed} / {counts.total}
        </span>{" "}
        <span className="muted small">passed</span>
        {run.is_terminal && executed < counts.total ? (
          <div className="small muted">{counts.total - executed} not executed</div>
        ) : null}
        <div className="small muted mono dashboard-tests" title={run.selected_test_ids.join(", ")}>
          {run.selected_test_ids.join(", ")}
        </div>
      </td>
      <td>
        {run.final_result ?? <span className="muted">—</span>}
        {run.error_message ? (
          <div className="small muted" title={run.error_message}>
            {truncate(run.error_message, 70)}
          </div>
        ) : null}
      </td>
      <td className="muted" title={run.started_at ?? run.created_at}>
        {formatTime(run.started_at ?? run.created_at)}
      </td>
      <td className="muted" title={run.finished_at ?? undefined}>
        {formatTime(run.finished_at)}
      </td>
      <td className="num mono">
        {run.is_terminal
          ? formatDuration(run.duration_seconds)
          : run.started_at
            ? elapsedSince(run.started_at, now)
            : "—"}
      </td>
    </tr>
  );
}

function truncate(text: string, max: number): string {
  return text.length > max ? `${text.slice(0, max - 1)}…` : text;
}
