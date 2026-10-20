import { describe, expect, it } from "vitest";
import { elapsedSince, formatDuration, formatMs, formatTime } from "./format";

describe("formatDuration", () => {
  it("shows tenths under 10 s, whole seconds under a minute, then minutes", () => {
    expect(formatDuration(0)).toBe("0.0s");
    expect(formatDuration(7.46)).toBe("7.5s");
    expect(formatDuration(12.3)).toBe("12s");
    expect(formatDuration(59.6)).toBe("60s");
    expect(formatDuration(60)).toBe("1m 0s");
    expect(formatDuration(135.4)).toBe("2m 15s");
  });

  it("renders a dash when the backend has no value yet", () => {
    expect(formatDuration(null)).toBe("—");
    expect(formatDuration(undefined)).toBe("—");
  });
});

describe("formatMs / formatTime / elapsedSince", () => {
  it("formats milliseconds as seconds with one decimal", () => {
    expect(formatMs(2000)).toBe("2.0s");
    expect(formatMs(1234)).toBe("1.2s");
    expect(formatMs(null)).toBe("—");
  });

  it("formats a missing timestamp as a dash", () => {
    expect(formatTime(null)).toBe("—");
    expect(formatTime("")).toBe("—");
    expect(formatTime("2026-10-20T10:00:05Z")).toMatch(/^\d{2}:\d{2}:\d{2}$/);
  });

  it("measures elapsed time from an ISO start and never goes negative", () => {
    const start = "2026-10-20T10:00:00Z";
    const startMs = new Date(start).getTime();
    expect(elapsedSince(start, startMs + 4_500)).toBe("4.5s");
    expect(elapsedSince(start, startMs + 90_000)).toBe("1m 30s");
    expect(elapsedSince(start, startMs - 5_000)).toBe("0.0s");
    expect(elapsedSince(null, startMs)).toBe("—");
  });
});
