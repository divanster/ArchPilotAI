import { act, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import AgentRunPanel from "./AgentRunPanel";
import type { AgentRun, PlanTask } from "./api";

const TASK = {
  key: "T-001",
  title: "Task",
  flags: [],
  blocking_questions: [],
} as unknown as PlanTask;

function run(over: Partial<AgentRun> = {}): AgentRun {
  return {
    id: "r1",
    task_key: "T-001",
    status: "running",
    stop_reason: null,
    stop_requested: false,
    max_steps: 20,
    steps_taken: 1,
    token_budget: 150000,
    input_tokens: 100,
    output_tokens: 20,
    time_limit_seconds: 300,
    provider: "fake",
    model: "m",
    prompt_version: "agent-v1",
    summary: null,
    patch_id: null,
    started_at: "2026-01-01T00:00:00Z",
    finished_at: null,
    duration_ms: 2500,
    steps: [
      {
        step_no: 1,
        action: "write_file",
        path: "src/a.py",
        status: "ok",
        error_code: null,
        result_summary: "wrote 6 byte(s)",
        input_tokens: 100,
        output_tokens: 20,
        latency_ms: 5,
      },
    ],
    ...over,
  };
}

function json(body: unknown, status = 200) {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

function setup(handler: (url: string, init?: RequestInit) => Response) {
  const fetchMock = vi.fn(async (url: string, init?: RequestInit) =>
    handler(url, init),
  );
  vi.stubGlobal("fetch", fetchMock);
  const onOpenPatch = vi.fn();
  render(
    <AgentRunPanel
      projectId="p"
      planId="pl"
      task={TASK}
      acknowledge={false}
      disabled={false}
      onOpenPatch={onOpenPatch}
    />,
  );
  return { fetchMock, onOpenPatch };
}

beforeEach(() => vi.useFakeTimers({ shouldAdvanceTime: true }));
afterEach(() => {
  vi.useRealTimers();
  vi.unstubAllGlobals();
});

describe("AgentRunPanel", () => {
  it("starts a run, polls the live step list and links the final patch", async () => {
    let polls = 0;
    const { onOpenPatch } = setup((url, init) => {
      if (init?.method === "POST")
        return json(run({ steps: [], steps_taken: 0 }), 202);
      if (url.endsWith("/agent-runs")) return json([]);
      polls += 1;
      return polls < 2
        ? json(run())
        : json(
            run({
              status: "finished",
              stop_reason: "finished",
              patch_id: "pat1",
              summary: "Added a module",
              steps_taken: 2,
              duration_ms: 4000,
            }),
          );
    });
    fireEvent.click(await screen.findByRole("button", { name: "Run agent" }));
    expect(await screen.findByText("running")).toBeTruthy();
    expect(screen.getByRole("button", { name: "Stop" })).toBeTruthy();

    await act(async () => {
      await vi.advanceTimersByTimeAsync(1600);
    });
    expect(await screen.findByText(/wrote 6 byte/)).toBeTruthy();
    expect(screen.getByText(/tokens 120\/150000/)).toBeTruthy();

    await act(async () => {
      await vi.advanceTimersByTimeAsync(1600);
    });
    expect(await screen.findByText("finished")).toBeTruthy();
    expect(screen.queryByRole("button", { name: "Stop" })).toBeNull();
    expect(screen.getByText(/4\.0 s/)).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "Open patch" }));
    expect(onOpenPatch).toHaveBeenCalledWith("pat1");
  });

  it("stops a running run", async () => {
    const { fetchMock } = setup((url, init) => {
      if (url.endsWith("/stop")) return json(run({ stop_requested: true }));
      if (init?.method === "POST") return json(run(), 202);
      return json([]);
    });
    fireEvent.click(await screen.findByRole("button", { name: "Run agent" }));
    fireEvent.click(await screen.findByRole("button", { name: "Stop" }));
    expect(
      await screen.findByRole("button", { name: "Stopping…" }),
    ).toBeTruthy();
    expect(
      fetchMock.mock.calls.some(([u]) =>
        String(u).endsWith("/agent-runs/r1/stop"),
      ),
    ).toBe(true);
  });

  it("shows step errors and why a run stopped", async () => {
    setup((url, init) => {
      if (init?.method === "POST") return json(run(), 202);
      if (url.endsWith("/agent-runs")) return json([]);
      return json(
        run({
          status: "stopped",
          stop_reason: "step_limit",
          steps: [
            {
              step_no: 1,
              action: "edit_file",
              path: "src/a.py",
              status: "error",
              error_code: "no_match",
              result_summary: "The search text was not found",
              input_tokens: 1,
              output_tokens: 1,
              latency_ms: 1,
            },
          ],
        }),
      );
    });
    fireEvent.click(await screen.findByRole("button", { name: "Run agent" }));
    expect(await screen.findByText("running")).toBeTruthy();
    await act(async () => {
      await vi.advanceTimersByTimeAsync(1600);
    });
    expect(await screen.findByText("no_match")).toBeTruthy();
    expect(screen.getByText(/stopped/)).toBeTruthy();
    expect(screen.getByText(/step_limit/)).toBeTruthy();
    expect(screen.queryByRole("button", { name: "Open patch" })).toBeNull();
  });

  it("reports a refused start (gate) without starting", async () => {
    setup((_url, init) => {
      if (init?.method === "POST")
        return json(
          { detail: { code: "plan_not_approved", message: "Plan is draft" } },
          409,
        );
      return json([]);
    });
    fireEvent.click(await screen.findByRole("button", { name: "Run agent" }));
    expect(await screen.findByRole("alert")).toBeTruthy();
    expect(screen.queryByText("running")).toBeNull();
  });

  it("resumes the latest run of the task on load", async () => {
    setup((url) => {
      if (url.endsWith("/agent-runs")) return json([run()]);
      return json(run());
    });
    expect(await screen.findByText("running")).toBeTruthy();
    expect(screen.getByRole("button", { name: "Stop" })).toBeTruthy();
  });
});
