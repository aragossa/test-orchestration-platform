import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import type { RunState, TestStatus } from "../lib/api";
import { RunStateBadge, TestStatusBadge } from "./StateBadge";

describe("RunStateBadge", () => {
  it.each<[RunState, string]>([
    ["PENDING", "badge "],
    ["PREPARING", "badge active"],
    ["RUNNING_TESTS", "badge active"],
    ["COMPLETED", "badge ok"],
    ["FAILED", "badge bad"],
    ["CANCELLED", "badge warn"],
  ])("%s gets the %s tone", (state, className) => {
    render(<RunStateBadge state={state} />);
    const badge = screen.getByText(state);
    expect(badge.className).toBe(className);
  });

  it("pulses only for active states when live", () => {
    const { container, rerender } = render(<RunStateBadge state="DEPLOYING" live />);
    expect(container.querySelector(".pulse")).not.toBeNull();
    rerender(<RunStateBadge state="COMPLETED" live />);
    expect(container.querySelector(".pulse")).toBeNull();
    rerender(<RunStateBadge state="DEPLOYING" />);
    expect(container.querySelector(".pulse")).toBeNull();
  });
});

describe("TestStatusBadge", () => {
  it.each<[TestStatus, string]>([
    ["PASSED", "badge ok"],
    ["FAILED", "badge bad"],
    ["TIMED_OUT", "badge bad"],
    ["SKIPPED", "badge warn"],
    ["RUNNING", "badge active"],
  ])("%s gets the %s tone", (status, className) => {
    render(<TestStatusBadge status={status} />);
    expect(screen.getByText(status).className).toBe(className);
  });
});
