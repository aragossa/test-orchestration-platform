import { useCallback, useEffect, useState } from "react";
import { Link, useParams } from "react-router-dom";
import { RunStateBadge, TestStatusBadge } from "../components/StateBadge";
import { api, ApiError, type RunDetail, type RunState } from "../lib/api";
import { elapsedSince, formatDateTime, formatDuration, formatMs, formatTime } from "../lib/format";
import { usePolling } from "../lib/usePolling";

const HAPPY_PATH: RunState[] = [
  "PENDING",
  "PREPARING",
  "DEPLOYING",
  "WAITING_FOR_READY",
  "RUNNING_TESTS",
  "COLLECTING_RESULTS",
  "COMPLETED",
];

const CANCELLABLE: RunState[] = ["PENDING", "PREPARING", "DEPLOYING", "WAITING_FOR_READY", "RUNNING_TESTS"];

export function RunDetails() {
  const { id = "" } = useParams();
  const [terminal, setTerminal] = useState(false);
  const load = useCallback(() => api.run(id), [id]);
  // Poll every second while the run is active; stop once it is terminal.
  const run = usePolling(load, 1000, !terminal);
  const [now, setNow] = useState(() => Date.now());
  const [cancelError, setCancelError] = useState<string | null>(null);
  const [cancelling, setCancelling] = useState(false);

  useEffect(() => {
    if (run.data?.is_terminal) setTerminal(true);
  }, [run.data?.is_terminal]);

  useEffect(() => {
    const t = window.setInterval(() => setNow(Date.now()), 1000);
    return () => window.clearInterval(t);
  }, []);

  if (run.error && !run.data) {
    return (
      <>
        <p>
          <Link to="/">← Back to runs</Link>
        </p>
        <div className="error-box">{run.error}</div>
      </>
    );
  }
  if (!run.data) return <div className="empty">Loading…</div>;
  const r = run.data;

  const cancel = async () => {
    setCancelError(null);
    setCancelling(true);
    try {
      await api.cancelRun(r.id);
      await run.refresh();
    } catch (err) {
      setCancelError(err instanceof ApiError ? err.message : String(err));
    } finally {
      setCancelling(false);
    }
  };

  const counts = r.test_counts;
  const canCancel = CANCELLABLE.includes(r.state) && !r.cancel_requested_at;

  return (
    <>
      <p style={{ margin: "0 0 10px" }}>
        <Link to="/">← Back to runs</Link>
      </p>
      <div className="page-header">
        <div style={{ display: "flex", alignItems: "center", gap: 12, flexWrap: "wrap" }}>
          <h1 className="mono">{r.label}</h1>
          <RunStateBadge state={r.state} live />
          {!r.is_terminal && r.current_step ? <span className="muted">{r.current_step}</span> : null}
          {r.cancel_requested_at && !r.is_terminal ? (
            <span className="badge warn">cancellation requested</span>
          ) : null}
        </div>
        <div style={{ display: "flex", gap: 10, alignItems: "center" }}>
          {run.error ? <span className="small muted">refresh failed: {run.error}</span> : null}
          {canCancel ? (
            <button className="danger" onClick={() => void cancel()} disabled={cancelling}>
              {cancelling ? "Cancelling…" : "Cancel run"}
            </button>
          ) : null}
        </div>
      </div>
      {cancelError ? <div className="error-box" style={{ marginBottom: 12 }}>{cancelError}</div> : null}

      {r.error_message ? (
        <div className="error-box" style={{ marginBottom: 16 }}>
          <strong>{r.state === "CANCELLED" ? "Cancelled" : "Failure reason"}:</strong> {r.error_message}
        </div>
      ) : null}

      <div className="stack">
        <div className="summary-strip">
          <Stat label="Environment" value={<code>{r.environment}</code>} />
          <Stat label="Tests passed" value={`${counts.passed} / ${counts.total}`} />
          <Stat
            label="Failures"
            value={counts.failed + counts.timed_out}
            sub={counts.skipped ? `${counts.skipped} skipped` : undefined}
          />
          <Stat
            label="Duration"
            value={
              r.is_terminal
                ? formatDuration(r.duration_seconds)
                : r.started_at
                  ? elapsedSince(r.started_at, now)
                  : "—"
            }
            sub={r.is_terminal ? undefined : "running"}
          />
          <Stat label="Started" value={formatTime(r.started_at)} sub={formatDateTime(r.started_at).split(" ")[0]} />
          <Stat label="Finished" value={formatTime(r.finished_at)} />
        </div>

        <div className="grid-2">
          <section className="card">
            <h2>Run configuration</h2>
            <dl className="kv">
              <dt>Environment</dt>
              <dd className="mono">{r.config.environment}</dd>
              <dt>Deployment version</dt>
              <dd className="mono">{r.config.deployment_version}</dd>
              <dt>Worker instances</dt>
              <dd>{r.config.worker_instances}</dd>
              <dt>Data generators</dt>
              <dd>{r.config.data_generators}</dd>
              <dt>Timeout</dt>
              <dd>{r.config.timeout_seconds} seconds</dd>
              <dt>Failure simulation</dt>
              <dd className="mono">{r.config.failure_simulation}</dd>
              <dt>Deployment id</dt>
              <dd className="mono">{r.deployment_id ?? "—"}</dd>
              <dt>Created</dt>
              <dd>{formatDateTime(r.created_at)}</dd>
              {r.worker_id ? (
                <>
                  <dt>Worker</dt>
                  <dd className="mono">{r.worker_id}</dd>
                </>
              ) : null}
            </dl>
            <p className="muted small" style={{ marginBottom: 0 }}>
              Immutable snapshot taken when the run was created. Later changes to environment defaults do not
              affect it.
            </p>
          </section>

          <section className="card">
            <h2>Execution timeline</h2>
            <StateTimeline run={r} />
          </section>
        </div>

        <section className="card">
          <h2>Test results</h2>
          <table>
            <thead>
              <tr>
                <th>#</th>
                <th>Test</th>
                <th>Category</th>
                <th>Status</th>
                <th className="num">Duration</th>
                <th>Started</th>
              </tr>
            </thead>
            <tbody>
              {r.tests.map((t) => (
                <tr key={t.test_id}>
                  <td className="muted">{t.position + 1}</td>
                  <td>
                    <div>{t.name}</div>
                    <div className="small muted mono">{t.test_id}</div>
                  </td>
                  <td>
                    <span className="badge category">{t.category}</span>
                  </td>
                  <td>
                    <TestStatusBadge status={t.status} />
                    {t.error_message ? <div className="test-row-detail bad">{t.error_message}</div> : null}
                    {t.message && !t.error_message ? <div className="test-row-detail">{t.message}</div> : null}
                  </td>
                  <td className="num mono">
                    {t.status === "RUNNING" && t.started_at ? elapsedSince(t.started_at, now) : formatMs(t.duration_ms)}
                  </td>
                  <td className="muted">{formatTime(t.started_at)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </section>

        <section className="card">
          <h2>Execution events</h2>
          <ul className="events">
            {r.events.map((e, i) => (
              <li key={i} className={e.level}>
                <span className="when">{formatTime(e.created_at)}</span>
                <span className="msg">{e.message}</span>
              </li>
            ))}
          </ul>
        </section>
      </div>
    </>
  );
}

function Stat({ label, value, sub }: { label: string; value: React.ReactNode; sub?: string }) {
  return (
    <div className="stat">
      <div className="stat-label">{label}</div>
      <div className="stat-value">{value}</div>
      {sub ? <div className="small muted">{sub}</div> : null}
    </div>
  );
}

/**
 * Shows the recorded transitions in order, followed by the happy-path states
 * that have not been reached yet (greyed out) so the viewer can see how far
 * along the run is. Terminal failures and cancellations end the list.
 */
function StateTimeline({ run }: { run: RunDetail }) {
  const recorded = run.transitions;
  const reached = new Set(recorded.map((t) => t.to_state));
  const lastIndex = recorded.length - 1;
  const remaining = run.is_terminal ? [] : HAPPY_PATH.filter((s) => !reached.has(s));

  return (
    <ul className="timeline">
      {recorded.map((t, i) => {
        const isLast = i === lastIndex;
        const bad = t.to_state === "FAILED" || t.to_state === "CANCELLED";
        const tickClass = bad ? "bad" : isLast && !run.is_terminal ? "now" : "done";
        const tick = bad ? "✕" : isLast && !run.is_terminal ? "●" : "✓";
        return (
          <li key={i}>
            <span className={`tick ${tickClass}`}>{tick}</span>
            <span className="when">{formatTime(t.created_at)}</span>
            <span>
              <span className="mono">{t.to_state}</span>
              {t.reason ? <span className="reason">{t.reason}</span> : null}
            </span>
          </li>
        );
      })}
      {remaining.map((s) => (
        <li key={s}>
          <span className="tick todo">○</span>
          <span className="when">—</span>
          <span className="mono muted">{s}</span>
        </li>
      ))}
    </ul>
  );
}
