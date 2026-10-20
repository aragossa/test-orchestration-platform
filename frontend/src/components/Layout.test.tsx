import { render, screen } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { describe, expect, it } from "vitest";
import { healthy, stubApi } from "../test/mockApi";
import { Layout } from "./Layout";

function renderLayout() {
  return render(
    <MemoryRouter initialEntries={["/"]}>
      <Routes>
        <Route element={<Layout />}>
          <Route index element={<div>page body</div>} />
        </Route>
      </Routes>
    </MemoryRouter>,
  );
}

describe("Layout health indicator", () => {
  it("reports live workers from /api/health", async () => {
    stubApi([{ path: "/api/health", body: healthy }]);
    renderLayout();
    expect(await screen.findByText("backend ok · 1 worker alive")).toBeTruthy();
    expect(screen.getByText("page body")).toBeTruthy();
  });

  it("says so when no worker is alive", async () => {
    stubApi([{ path: "/api/health", body: { ...healthy, status: "degraded", workers: [] } }]);
    renderLayout();
    expect(await screen.findByText("degraded: no workers alive")).toBeTruthy();
  });

  it("says so when the backend cannot be reached", async () => {
    stubApi([{ path: "/api/health", status: 503, body: { error: "db down" } }]);
    renderLayout();
    expect(await screen.findByText("backend unreachable")).toBeTruthy();
  });
});
