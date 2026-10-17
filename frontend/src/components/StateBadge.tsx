import type { RunState, TestStatus } from "../lib/api";

const RUN_TONE: Record<RunState, string> = {
  PENDING: "",
  PREPARING: "active",
  DEPLOYING: "active",
  WAITING_FOR_READY: "active",
  RUNNING_TESTS: "active",
  COLLECTING_RESULTS: "active",
  COMPLETED: "ok",
  FAILED: "bad",
  CANCELLED: "warn",
};

const TEST_TONE: Record<TestStatus, string> = {
  PENDING: "",
  RUNNING: "active",
  PASSED: "ok",
  FAILED: "bad",
  TIMED_OUT: "bad",
  SKIPPED: "warn",
};

export function RunStateBadge({ state, live = false }: { state: RunState; live?: boolean }) {
  const tone = RUN_TONE[state];
  return (
    <span className={`badge ${tone}`}>
      {live && tone === "active" ? <span className="pulse" aria-hidden /> : null}
      {state}
    </span>
  );
}

export function TestStatusBadge({ status }: { status: TestStatus }) {
  return (
    <span className={`badge ${TEST_TONE[status]}`}>
      {status === "RUNNING" ? <span className="pulse" aria-hidden /> : null}
      {status}
    </span>
  );
}
