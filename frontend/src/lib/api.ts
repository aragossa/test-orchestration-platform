// Typed client for the backend API. Everything the UI shows comes from here;
// the UI never simulates execution itself.

export type RunState =
  | "PENDING"
  | "PREPARING"
  | "DEPLOYING"
  | "WAITING_FOR_READY"
  | "RUNNING_TESTS"
  | "COLLECTING_RESULTS"
  | "COMPLETED"
  | "FAILED"
  | "CANCELLED";

export type TestStatus = "PENDING" | "RUNNING" | "PASSED" | "FAILED" | "TIMED_OUT" | "SKIPPED";

export type FailureSimulation = "none" | "deployment_failure" | "test_failure" | "test_timeout";

export const RUN_STATES: RunState[] = [
  "PENDING",
  "PREPARING",
  "DEPLOYING",
  "WAITING_FOR_READY",
  "RUNNING_TESTS",
  "COLLECTING_RESULTS",
  "COMPLETED",
  "FAILED",
  "CANCELLED",
];

export const FAILURE_SIMULATIONS: { value: FailureSimulation; label: string; hint: string }[] = [
  { value: "none", label: "none", hint: "No fault injected. Mock operations succeed." },
  {
    value: "deployment_failure",
    label: "deployment_failure",
    hint: "Deployment fails; the run stops before any test executes.",
  },
  {
    value: "test_failure",
    label: "test_failure",
    hint: "The first selected test fails; the rest still run.",
  },
  {
    value: "test_timeout",
    label: "test_timeout",
    hint: "The first selected test hangs until the run timeout expires. Pick a short timeout (5–10 s) to see it quickly.",
  },
];

export interface Environment {
  id: string;
  display_name: string;
  default_worker_instances: number;
  default_data_generators: number;
  default_deployment_version: string;
  default_timeout_seconds: number;
  updated_at: string;
}

export interface TestDefinition {
  id: string;
  name: string;
  category: string;
  description: string;
  base_duration_ms: number;
}

export interface TestCounts {
  total: number;
  passed: number;
  failed: number;
  timed_out: number;
  skipped: number;
  pending: number;
}

export interface RunSummary {
  id: number;
  label: string;
  environment: string;
  state: RunState;
  is_terminal: boolean;
  current_step: string | null;
  final_result: string | null;
  error_message: string | null;
  failure_simulation: FailureSimulation;
  selected_test_ids: string[];
  test_counts: TestCounts;
  created_at: string;
  started_at: string | null;
  finished_at: string | null;
  duration_seconds: number | null;
}

export interface RunTest {
  test_id: string;
  name: string;
  category: string;
  position: number;
  status: TestStatus;
  duration_ms: number | null;
  message: string | null;
  error_message: string | null;
  started_at: string | null;
  finished_at: string | null;
}

export interface Transition {
  from_state: RunState | null;
  to_state: RunState;
  reason: string | null;
  created_at: string;
}

export interface RunEvent {
  level: "info" | "warning" | "error";
  message: string;
  payload: Record<string, unknown> | null;
  created_at: string;
}

export interface RunDetail extends RunSummary {
  config: {
    environment: string;
    deployment_version: string;
    worker_instances: number;
    data_generators: number;
    timeout_seconds: number;
    selected_test_ids: string[];
    failure_simulation: FailureSimulation;
  };
  deployment_id: string | null;
  worker_id: string | null;
  cancel_requested_at: string | null;
  tests: RunTest[];
  transitions: Transition[];
  events: RunEvent[];
}

export interface RunList {
  items: RunSummary[];
  total: number;
  limit: number;
  offset: number;
}

export interface RunCreate {
  environment: string;
  deployment_version: string;
  worker_instances: number;
  data_generators: number;
  timeout_seconds: number;
  selected_test_ids: string[];
  failure_simulation: FailureSimulation;
}

export interface Health {
  status: string;
  database: string;
  worker: { worker_id?: string; running: boolean; active_runs?: number[] };
  version: string;
}

export interface ApiErrorDetail {
  field: string | null;
  message: string;
}

export class ApiError extends Error {
  status: number;
  details: ApiErrorDetail[];

  constructor(status: number, message: string, details: ApiErrorDetail[] = []) {
    super(message);
    this.status = status;
    this.details = details;
  }
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(`/api${path}`, {
    headers: { "Content-Type": "application/json", ...(init?.headers ?? {}) },
    ...init,
  });
  if (!response.ok) {
    let message = `${response.status} ${response.statusText}`;
    let details: ApiErrorDetail[] = [];
    try {
      const body = await response.json();
      if (body?.error) message = body.error;
      if (Array.isArray(body?.details)) details = body.details;
    } catch {
      // body was not JSON; keep the status text
    }
    throw new ApiError(response.status, message, details);
  }
  return (await response.json()) as T;
}

export const api = {
  environments: () => request<Environment[]>("/environments"),
  tests: () => request<TestDefinition[]>("/tests"),
  runs: (params: { environment?: string; state?: RunState | ""; limit?: number } = {}) => {
    const query = new URLSearchParams();
    if (params.environment) query.set("environment", params.environment);
    if (params.state) query.set("state", params.state);
    query.set("limit", String(params.limit ?? 50));
    return request<RunList>(`/runs?${query.toString()}`);
  },
  run: (id: number | string) => request<RunDetail>(`/runs/${id}`),
  createRun: (body: RunCreate) =>
    request<RunDetail>("/runs", { method: "POST", body: JSON.stringify(body) }),
  cancelRun: (id: number) => request<RunDetail>(`/runs/${id}/cancel`, { method: "POST" }),
  health: () => request<Health>("/health"),
};
