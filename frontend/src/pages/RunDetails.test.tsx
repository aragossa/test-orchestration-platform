import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { describe, expect, it } from "vitest";
import type { RunDetail } from "../lib/api";
import { makeRun, stubApi } from "../test/mockApi";
import { RunDetails } from "./RunDetails";

function renderDetails(id = 7) {
  return render(
    <MemoryRouter initialEntries={[`/runs/${id}`]}>
      <Routes>
        <Route path="runs/:id" element={<RunDetails />} />
      </Routes>
    </MemoryRouter>,
  );
}

function section(title: string): HTMLElement {
  const heading = screen.getByRole("heading", { name: title });
  return heading.closest("section") as HTMLElement;
}

describe("RunDetails", () => {
  it("renders a completed run: configuration snapshot, timeline, tests and events", async () => {
    stubApi([{ path: "/api/runs/7", body: makeRun() }]);
    renderDetails();

    expect(await screen.findByRole("heading", { name: "run-7" })).toBeTruthy();
    expect(screen.getByText("COMPLETED", { selector: ".badge" })).toBeTruthy();
    expect(screen.queryByRole("button", { name: /Cancel run/ })).toBeNull();

    const config = section("Run configuration");
    expect(config.textContent).toContain("latest");
    expect(config.textContent).toContain("dep-abc123");
    expect(config.textContent).toContain("60 seconds");

    const timeline = section("Execution timeline");
    const items = within(timeline).getAllByRole("listitem");
    expect(items.map((li) => li.querySelector(".mono")?.textContent)).toEqual([
      "PENDING",
      "PREPARING",
      "DEPLOYING",
      "WAITING_FOR_READY",
      "RUNNING_TESTS",
      "COLLECTING_RESULTS",
      "COMPLETED",
    ]);
    expect(timeline.querySelectorAll(".tick.todo")).toHaveLength(0);

    const tests = section("Test results");
    expect(within(tests).getAllByText("PASSED")).toHaveLength(3);
    expect(tests.textContent).toContain("Connectivity Check");
    expect(tests.textContent).toContain("2.0s");

    expect(section("Execution events").textContent).toContain("Run completed successfully");
    expect(screen.getByText("3 / 3")).toBeTruthy();
  });

  it("renders a failed deployment: reason, skipped tests and a failed tick", async () => {
    const failed: RunDetail = makeRun({
      state: "FAILED",
      final_result: "Failed before tests ran",
      error_message: "Simulated deployment failure: image 'latest' failed health checks on test",
      deployment_id: null,
      test_counts: { total: 3, passed: 0, failed: 0, timed_out: 0, skipped: 3, pending: 0 },
      tests: makeRun().tests.map((t) => ({
        ...t,
        status: "SKIPPED",
        duration_ms: null,
        message: "Skipped: run failed before execution",
        started_at: null,
      })),
      transitions: makeRun()
        .transitions.slice(0, 3)
        .concat([
          {
            from_state: "DEPLOYING",
            to_state: "FAILED",
            reason: "Simulated deployment failure: image 'latest' failed health checks on test",
            created_at: "2026-10-20T10:00:09Z",
          },
        ]),
    });
    stubApi([{ path: "/api/runs/7", body: failed }]);
    renderDetails();

    await screen.findByRole("heading", { name: "run-7" });
    expect(screen.getByText("Failure reason:").parentElement?.textContent).toContain(
      "Simulated deployment failure",
    );
    const tests = section("Test results");
    expect(within(tests).getAllByText("SKIPPED")).toHaveLength(3);
    expect(within(tests).getAllByText("Skipped: run failed before execution")).toHaveLength(3);
    expect(screen.getByText("3 skipped")).toBeTruthy();

    const timeline = section("Execution timeline");
    expect(timeline.querySelectorAll(".tick.bad")).toHaveLength(1);
    expect(timeline.querySelectorAll(".tick.todo")).toHaveLength(0); // terminal: nothing "to do"
    expect(timeline.textContent).toContain("FAILED");
  });

  it("shows the remaining happy-path steps and a cancel button while active, and cancels", async () => {
    const active: RunDetail = makeRun({
      state: "DEPLOYING",
      is_terminal: false,
      current_step: "Deploying latest",
      final_result: null,
      finished_at: null,
      duration_seconds: null,
      deployment_id: null,
      worker_id: "node-1",
      test_counts: { total: 3, passed: 0, failed: 0, timed_out: 0, skipped: 0, pending: 3 },
      tests: makeRun().tests.map((t) => ({ ...t, status: "PENDING", duration_ms: null, started_at: null })),
      transitions: makeRun().transitions.slice(0, 3),
    });
    const cancelRequested: RunDetail = { ...active, cancel_requested_at: "2026-10-20T10:00:10Z" };
    let current = active;
    const api = stubApi([
      { path: "/api/runs/7", body: () => current },
      {
        method: "POST",
        path: "/api/runs/7/cancel",
        body: () => {
          current = cancelRequested;
          return cancelRequested;
        },
      },
    ]);
    renderDetails();

    await screen.findByRole("heading", { name: "run-7" });
    expect(screen.getByText("Deploying latest")).toBeTruthy();
    expect(section("Run configuration").textContent).toContain("node-1");

    const timeline = section("Execution timeline");
    expect(timeline.querySelectorAll(".tick.now")).toHaveLength(1);
    expect(timeline.querySelectorAll(".tick.todo")).toHaveLength(4); // WAITING_FOR_READY … COMPLETED

    fireEvent.click(screen.getByRole("button", { name: "Cancel run" }));
    await waitFor(() => expect(screen.getByText("cancellation requested")).toBeTruthy());
    expect(api.calls().some((c) => c.method === "POST" && c.path === "/api/runs/7/cancel")).toBe(true);
    expect(screen.queryByRole("button", { name: /Cancel run/ })).toBeNull();
  });

  it("shows the API error for an unknown run", async () => {
    stubApi([{ path: "/api/runs/404", status: 404, body: { error: "Run 404 not found" } }]);
    renderDetails(404);
    expect(await screen.findByText("Run 404 not found")).toBeTruthy();
    expect(screen.getByText("← Back to runs")).toBeTruthy();
  });
});
