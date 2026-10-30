import { act, fireEvent, render, screen, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import AgentRunPanel from "./AgentRunPanel";
import type { AgentHook, AgentRun, PlanTask } from "./api";

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
        findings: [],
        finding_total: null,
        findings_not_shown: 0,
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

  it("shows the tool name and tier of each step", async () => {
    const base = run().steps[0];
    setup((url, init) => {
      if (init?.method === "POST") return json(run(), 202);
      if (url.endsWith("/agent-runs")) return json([]);
      return json(
        run({
          steps: [
            { ...base, step_no: 1, action: "read_file", tier: "read" },
            { ...base, step_no: 2, action: "write_file", tier: "draft" },
          ],
        }),
      );
    });
    fireEvent.click(await screen.findByRole("button", { name: "Run agent" }));
    expect(await screen.findByText("running")).toBeTruthy();
    await act(async () => {
      await vi.advanceTimersByTimeAsync(1600);
    });
    await screen.findByText("read_file");
    const steps = within(screen.getByRole("list", { name: "Agent steps" }));
    const items = steps.getAllByRole("listitem");
    expect(items[0].textContent).toMatch(/read_file\s*read/);
    expect(items[1].textContent).toMatch(/write_file\s*draft/);
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
              findings: [],
              finding_total: null,
              findings_not_shown: 0,
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

  it("lists the findings of a static check and what was left out", async () => {
    setup((url) => {
      if (url.endsWith("/agent-runs"))
        return json([run({ status: "finished" })]);
      return json(
        run({
          status: "stopped",
          stop_reason: "not_converging",
          steps: [
            {
              step_no: 1,
              action: "run_static_checks",
              path: null,
              status: "ok",
              error_code: null,
              result_summary: "3 finding(s)",
              input_tokens: 1,
              output_tokens: 1,
              latency_ms: 1,
              findings: [
                {
                  tool: "ruff",
                  file: "src/a.py",
                  line: 7,
                  code: "F401",
                  message: "os imported but unused",
                },
              ],
              finding_total: 3,
              findings_not_shown: 4,
            },
          ],
        }),
      );
    });
    expect(await screen.findByText("src/a.py:7")).toBeTruthy();
    expect(screen.getByText("ruff F401")).toBeTruthy();
    expect(screen.getByText(/os imported but unused/)).toBeTruthy();
    expect(screen.getByText(/2 more not shown/)).toBeTruthy();
    expect(screen.getByText(/4 environment finding/)).toBeTruthy();
    expect(screen.getByText(/not_converging/)).toBeTruthy();
  });

  it("shows the checker status before a run and blocks runs when one is missing", async () => {
    setup((url) => {
      if (url.endsWith("/checks/status"))
        return json({
          ok: false,
          tools: {
            ruff: { available: true, version: "ruff 0.17.3" },
            mypy: { available: false, version: null },
          },
        });
      return json([]);
    });
    expect(await screen.findByText(/ruff ruff 0.17.3/)).toBeTruthy();
    expect(screen.getByText(/mypy UNAVAILABLE/)).toBeTruthy();
    expect(
      (screen.getByRole("button", { name: "Run agent" }) as HTMLButtonElement)
        .disabled,
    ).toBe(true);
  });

  it("explains a run stopped by unavailable checkers", async () => {
    setup((url) => {
      if (url.endsWith("/agent-runs")) return json([run()]);
      return json(
        run({ status: "stopped", stop_reason: "checks_unavailable" }),
      );
    });
    expect(
      await screen.findByText(
        /static checker \(ruff or mypy\) is not available/,
      ),
    ).toBeTruthy();
  });

  it("shows test results, attribution and the jobs of the run", async () => {
    const withTests = run({
      status: "finished",
      steps: [
        {
          ...run().steps[0],
          action: "run_tests",
          path: null,
          result_summary: "pytest: failed",
          tests: {
            command_id: "pytest",
            status: "failed",
            structured: true,
            new: [{ name: "tests.t::test_x", message: "boom", file: "t.py" }],
            new_in_changed: 1,
            new_elsewhere: 2,
            pre_existing: 3,
            fixed: 0,
            flaky: ["tests.t::test_y"],
            baseline: true,
            note: null,
          },
        },
      ],
      jobs: [
        {
          id: "j1",
          command_id: "pytest",
          variant: "changed",
          status: "timed_out",
          timeout_seconds: 120,
          exit_code: null,
          duration_ms: 120000,
          truncated: true,
          select_test: null,
          flaky: null,
        },
      ],
    });
    setup((url) =>
      url.endsWith("/agent-runs") ? json([withTests]) : json(withTests),
    );
    expect(
      await screen.findByText(/1 new in changed files · 2 new elsewhere/),
    ).toBeTruthy();
    expect(screen.getByText(/3 pre-existing/)).toBeTruthy();
    expect(screen.getByText(/Flaky \(passed on re-run\)/)).toBeTruthy();
    const jobs = screen.getByLabelText("Jobs");
    expect(jobs.textContent).toContain("timed_out");
    expect(jobs.textContent).toContain("output truncated");
  });

  it("lists hooks with required ones locked, toggles optional ones and shows decisions", async () => {
    const hooks: AgentHook[] = [
      {
        name: "secret_scan",
        point: "before_action",
        tools: ["write_file"],
        order: 20,
        required: true,
        description: "Blocks a write with a secret in it.",
        enabled: true,
      },
      {
        name: "static_checks",
        point: "after_action",
        tools: ["write_file"],
        order: 10,
        required: false,
        description: "Runs the static checks after an edit.",
        enabled: true,
      },
    ];
    const base = run().steps[0];
    const withHooks = run({
      status: "finished",
      hooks,
      steps: [
        {
          ...base,
          hooks: [
            {
              hook: "static_checks",
              point: "after_action",
              result: "annotate",
              reason: "2 finding(s)",
            },
          ],
        },
      ],
    });
    const puts: string[] = [];
    setup((url, init) => {
      if (url.endsWith("/agent-hooks") || url.includes("/agent-hooks/")) {
        if (init?.method === "PUT") {
          puts.push(String(init.body));
          return json(
            hooks.map((h) =>
              h.name === "static_checks" ? { ...h, enabled: false } : h,
            ),
          );
        }
        return json(hooks);
      }
      return url.endsWith("/agent-runs") ? json([withHooks]) : json(withHooks);
    });
    const secret = (await screen.findByLabelText(
      /secret_scan/,
    )) as HTMLInputElement;
    expect(secret.disabled).toBe(true);
    expect(screen.getByText("locked")).toBeTruthy();
    expect(
      (await screen.findByLabelText("Active hooks of the run")).textContent,
    ).toContain("secret_scan, static_checks");
    expect(
      within(screen.getByLabelText("Hooks of step 1")).getByText("annotate"),
    ).toBeTruthy();

    const optional = screen.getByLabelText(/static_checks/) as HTMLInputElement;
    fireEvent.click(optional);
    await act(async () => {
      await vi.advanceTimersByTimeAsync(10);
    });
    expect(puts).toEqual([JSON.stringify({ enabled: false })]);
    expect(
      (screen.getByLabelText(/static_checks/) as HTMLInputElement).checked,
    ).toBe(false);
  });
});
