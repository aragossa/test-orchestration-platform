import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { describe, expect, it } from "vitest";
import { catalog, environments, makeRun, stubApi, type Route as ApiRoute } from "../test/mockApi";
import { NewRun } from "./NewRun";

const reference: ApiRoute[] = [
  { path: "/api/environments", body: environments },
  { path: "/api/tests", body: catalog },
];

function renderNewRun() {
  return render(
    <MemoryRouter initialEntries={["/runs/new"]}>
      <Routes>
        <Route path="runs/new" element={<NewRun />} />
        <Route path="runs/:id" element={<div>opened run page</div>} />
      </Routes>
    </MemoryRouter>,
  );
}

const input = (label: RegExp | string) => screen.getByLabelText(label) as HTMLInputElement;

describe("NewRun", () => {
  it("loads defaults from the backend for the selected environment", async () => {
    stubApi(reference);
    renderNewRun();

    expect(await screen.findByRole("button", { name: "test", pressed: true })).toBeTruthy();
    expect(input("Deployment version").value).toBe("latest");
    expect(input(/Worker instances/).value).toBe("1");
    expect(input(/Test timeout/).value).toBe("60");
    expect(screen.getByText("(0 of 3 selected)")).toBeTruthy();

    fireEvent.click(screen.getByRole("button", { name: "stage" }));
    expect(input("Deployment version").value).toBe("v2.0.0");
    expect(input(/Worker instances/).value).toBe("3");
    expect(input(/Data generators/).value).toBe("2");
    expect(input(/Test timeout/).value).toBe("120");
    expect(screen.queryByText("overridden")).toBeNull();

    fireEvent.change(input(/Worker instances/), { target: { value: "5" } });
    expect(screen.getAllByText("overridden")).toHaveLength(1);
  });

  it("refuses to submit until at least one test is selected", async () => {
    stubApi(reference);
    renderNewRun();
    await screen.findByRole("button", { name: "test", pressed: true });

    const start = screen.getByRole("button", { name: "Start run" }) as HTMLButtonElement;
    expect(start.disabled).toBe(true);
    fireEvent.click(screen.getByLabelText(/Connectivity Check/));
    expect(start.disabled).toBe(false);
    fireEvent.change(input(/Worker instances/), { target: { value: "9" } });
    expect(start.disabled).toBe(true);
  });

  it("posts the configuration the backend expects and opens the new run", async () => {
    const api = stubApi([
      ...reference,
      { method: "POST", path: "/api/runs", body: makeRun({ id: 42 }) },
    ]);
    renderNewRun();
    await screen.findByRole("button", { name: "test", pressed: true });

    fireEvent.click(screen.getByLabelText(/Processing Validation/));
    fireEvent.click(screen.getByLabelText(/Connectivity Check/));
    fireEvent.change(input(/Test timeout/), { target: { value: "5" } });
    fireEvent.click(screen.getByLabelText(/test_timeout/));
    fireEvent.click(screen.getByRole("button", { name: "Start run" }));

    expect(await screen.findByText("opened run page")).toBeTruthy();
    const post = api.calls().find((c) => c.method === "POST");
    expect(post?.body).toEqual({
      environment: "test",
      deployment_version: "latest",
      worker_instances: 1,
      data_generators: 1,
      timeout_seconds: 5,
      // Catalog order, not click order.
      selected_test_ids: ["connectivity_check", "processing_validation"],
      failure_simulation: "test_timeout",
    });
  });

  it("shows backend validation errors next to the field", async () => {
    stubApi([
      ...reference,
      {
        method: "POST",
        path: "/api/runs",
        status: 422,
        body: {
          error: "Invalid run configuration",
          details: [{ field: "selected_test_ids", message: "Unknown test id(s): ghost" }],
        },
      },
    ]);
    renderNewRun();
    await screen.findByRole("button", { name: "test", pressed: true });
    fireEvent.click(screen.getByLabelText(/Data Ingestion/));
    fireEvent.click(screen.getByRole("button", { name: "Start run" }));

    expect(await screen.findByText("Invalid run configuration")).toBeTruthy();
    expect(screen.getByText("Unknown test id(s): ghost")).toBeTruthy();
    await waitFor(() =>
      expect((screen.getByRole("button", { name: "Start run" }) as HTMLButtonElement).disabled).toBe(false),
    );
  });
});
