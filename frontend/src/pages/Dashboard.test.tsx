import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { describe, expect, it } from "vitest";
import { makeRun, stubApi, toSummary } from "../test/mockApi";
import { Dashboard } from "./Dashboard";

function renderDashboard() {
  return render(
    <MemoryRouter initialEntries={["/"]}>
      <Routes>
        <Route index element={<Dashboard />} />
        <Route path="runs/:id" element={<div>run page</div>} />
      </Routes>
    </MemoryRouter>,
  );
}

describe("Dashboard", () => {
  it("shows the empty state when the API has no runs", async () => {
    stubApi([{ path: "/api/runs", body: { items: [], total: 0, limit: 50, offset: 0 } }]);
    renderDashboard();
    expect(await screen.findByText(/No runs yet/)).toBeTruthy();
    expect(screen.getByText("Create the first one.").getAttribute("href")).toBe("/runs/new");
  });

  it("renders one row per run with state, counts and result from the API", async () => {
    const completed = toSummary(makeRun());
    const failed = toSummary(
      makeRun({
        id: 8,
        label: "run-8",
        environment: "stage",
        state: "FAILED",
        final_result: "Failed before tests ran",
        error_message: "Simulated deployment failure: image 'v2' failed health checks",
        test_counts: { total: 3, passed: 0, failed: 0, timed_out: 0, skipped: 3, pending: 0 },
      }),
    );
    const running = toSummary(
      makeRun({
        id: 9,
        label: "run-9",
        state: "RUNNING_TESTS",
        is_terminal: false,
        current_step: "Running test 2 of 3: Data Ingestion",
        final_result: null,
        finished_at: null,
        duration_seconds: null,
        test_counts: { total: 3, passed: 1, failed: 0, timed_out: 0, skipped: 0, pending: 2 },
      }),
    );
    stubApi([
      { path: "/api/runs", body: { items: [running, failed, completed], total: 3, limit: 50, offset: 0 } },
    ]);
    renderDashboard();

    expect(await screen.findByText("3 runs · refreshes every second")).toBeTruthy();
    const rows = screen.getAllByRole("row").slice(1); // skip the header
    expect(rows).toHaveLength(3);

    expect(rows[0].textContent).toContain("run-9");
    expect(rows[0].textContent).toContain("RUNNING_TESTS");
    expect(rows[0].textContent).toContain("Running test 2 of 3: Data Ingestion");
    expect(rows[0].textContent).toContain("1 / 3");

    expect(rows[1].textContent).toContain("FAILED");
    expect(rows[1].textContent).toContain("Failed before tests ran");
    expect(rows[1].textContent).toContain("3 not executed");
    expect(rows[1].textContent).toContain("Simulated deployment failure");

    expect(rows[2].textContent).toContain("COMPLETED");
    expect(rows[2].textContent).toContain("3/3 tests passed");
    expect(rows[2].textContent).toContain("12s");
  });

  it("passes the selected filters to the API", async () => {
    const api = stubApi([{ path: "/api/runs", body: { items: [], total: 0, limit: 50, offset: 0 } }]);
    renderDashboard();
    await screen.findByText(/No runs yet/);

    fireEvent.change(screen.getByLabelText("Environment"), { target: { value: "stage" } });
    fireEvent.change(screen.getByLabelText("Status"), { target: { value: "FAILED" } });

    await waitFor(() => {
      const paths = api.calls().map((c) => c.path);
      expect(paths).toContain("/api/runs?environment=stage&state=FAILED&limit=50");
    });
  });

  it("shows the API error without hiding the page", async () => {
    stubApi([{ path: "/api/runs", status: 500, body: { error: "database unavailable" } }]);
    renderDashboard();
    expect(await screen.findByText("Could not load runs: database unavailable")).toBeTruthy();
    expect(screen.getByText("+ Create new run")).toBeTruthy();
  });
});
