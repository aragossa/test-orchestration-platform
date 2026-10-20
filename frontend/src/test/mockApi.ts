// A fake backend for component tests: fixtures shaped exactly like the API
// responses, and a `fetch` stub that routes by method + path. The UI renders
// what the API returns, so the tests feed it API payloads and check the DOM.

import { vi } from "vitest";
import type { Environment, Health, RunDetail, RunSummary, TestDefinition } from "../lib/api";

export const environments: Environment[] = [
  {
    id: "stage",
    display_name: "Stage",
    default_worker_instances: 3,
    default_data_generators: 2,
    default_deployment_version: "v2.0.0",
    default_timeout_seconds: 120,
    updated_at: "2026-10-20T10:00:00Z",
  },
  {
    id: "test",
    display_name: "Test",
    default_worker_instances: 1,
    default_data_generators: 1,
    default_deployment_version: "latest",
    default_timeout_seconds: 60,
    updated_at: "2026-10-20T10:00:00Z",
  },
];

export const catalog: TestDefinition[] = [
  {
    id: "connectivity_check",
    name: "Connectivity Check",
    category: "Smoke",
    description: "Verify that deployed services are reachable",
    base_duration_ms: 2000,
  },
  {
    id: "data_ingestion",
    name: "Data Ingestion",
    category: "Functional",
    description: "Push sample data through the pipeline",
    base_duration_ms: 4000,
  },
  {
    id: "processing_validation",
    name: "Processing Validation",
    category: "Functional",
    description: "Check processed output",
    base_duration_ms: 3000,
  },
];

export const healthy: Health = {
  status: "ok",
  database: "ok",
  version: "0.1.0",
  workers: [
    {
      worker_id: "node-1",
      concurrency: 4,
      active_runs: 1,
      started_at: "2026-10-20T10:00:00Z",
      last_heartbeat_at: "2026-10-20T10:05:00Z",
    },
  ],
};

const T0 = "2026-10-20T10:00:00Z";

/** A COMPLETED run as the API returns it; override fields per test. */
export function makeRun(overrides: Partial<RunDetail> = {}): RunDetail {
  const base: RunDetail = {
    id: 7,
    label: "run-7",
    environment: "test",
    state: "COMPLETED",
    is_terminal: true,
    current_step: null,
    final_result: "3/3 tests passed",
    error_message: null,
    failure_simulation: "none",
    selected_test_ids: catalog.map((t) => t.id),
    test_counts: { total: 3, passed: 3, failed: 0, timed_out: 0, skipped: 0, pending: 0 },
    created_at: T0,
    started_at: "2026-10-20T10:00:01Z",
    finished_at: "2026-10-20T10:00:13Z",
    duration_seconds: 12.3,
    config: {
      environment: "test",
      deployment_version: "latest",
      worker_instances: 1,
      data_generators: 1,
      timeout_seconds: 60,
      selected_test_ids: catalog.map((t) => t.id),
      failure_simulation: "none",
    },
    deployment_id: "dep-abc123",
    worker_id: null,
    cancel_requested_at: null,
    tests: catalog.map((t, i) => ({
      test_id: t.id,
      name: t.name,
      category: t.category,
      position: i,
      status: "PASSED",
      duration_ms: 2000 + i,
      message: "ok",
      error_message: null,
      started_at: "2026-10-20T10:00:05Z",
      finished_at: "2026-10-20T10:00:07Z",
    })),
    transitions: [
      "PENDING",
      "PREPARING",
      "DEPLOYING",
      "WAITING_FOR_READY",
      "RUNNING_TESTS",
      "COLLECTING_RESULTS",
      "COMPLETED",
    ].map((s, i, all) => ({
      from_state: i === 0 ? null : (all[i - 1] as RunDetail["state"]),
      to_state: s as RunDetail["state"],
      reason: null,
      created_at: T0,
    })),
    events: [
      { level: "info", message: "Run created", payload: null, created_at: T0 },
      { level: "info", message: "Run completed successfully: 3/3 tests passed", payload: null, created_at: T0 },
    ],
  };
  return { ...base, ...overrides };
}

export function toSummary(run: RunDetail): RunSummary {
  const { config: _c, deployment_id: _d, worker_id: _w, cancel_requested_at: _r, tests: _t, transitions: _tr, events: _e, ...summary } = run;
  return summary;
}

export interface Route {
  method?: string;
  path: string | RegExp;
  status?: number;
  body: unknown | ((url: URL, init?: RequestInit) => unknown);
}

export interface FetchStub {
  calls: () => { method: string; path: string; body: unknown }[];
}

/**
 * Replace global fetch with a router over `routes`. Unmatched requests get a
 * 404 so a test fails loudly instead of hanging. Returns the recorded calls.
 */
export function stubApi(routes: Route[]): FetchStub {
  const calls: { method: string; path: string; body: unknown }[] = [];
  const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = new URL(String(input), "http://localhost");
    const method = (init?.method ?? "GET").toUpperCase();
    const body = typeof init?.body === "string" ? JSON.parse(init.body) : null;
    calls.push({ method, path: url.pathname + url.search, body });
    const route = routes.find(
      (r) =>
        (r.method ?? "GET").toUpperCase() === method &&
        (typeof r.path === "string" ? r.path === url.pathname : r.path.test(url.pathname)),
    );
    const status = route?.status ?? (route ? 200 : 404);
    const payload = route
      ? typeof route.body === "function"
        ? (route.body as (u: URL, i?: RequestInit) => unknown)(url, init)
        : route.body
      : { error: `No stub for ${method} ${url.pathname}` };
    return {
      ok: status >= 200 && status < 300,
      status,
      statusText: status === 200 ? "OK" : "Error",
      json: async () => payload,
    } as Response;
  });
  vi.stubGlobal("fetch", fetchMock);
  return { calls: () => calls };
}

/** Routes every page needs because the layout polls /api/health. */
export const healthRoute: Route = { path: "/api/health", body: healthy };
