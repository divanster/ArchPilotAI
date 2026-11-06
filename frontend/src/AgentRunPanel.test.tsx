import { act, fireEvent, render, screen, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import AgentRunPanel from "./AgentRunPanel";
import { AdvancedProvider } from "./advanced";
import type {
  AgentHook,
  AgentJob,
  AgentRun,
  CILive,
  CiLoopApprovalInfo,
  CiLoopResult,
  CiLoopView,
  PlanTask,
} from "./api";

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
  it("shows which skills a run loaded", async () => {
    setup((url, init) => {
      if (init?.method === "POST") return json(run(), 202);
      if (url.endsWith("/agent-runs")) return json([run()]);
      if (url.endsWith("/agent-skills"))
        return json({ skills: [], problems: [] });
      return json(
        run({
          status: "finished",
          skills: [
            {
              name: "code-review",
              source: "vendored",
              version: "4ae2317",
              content_hash: "c".repeat(64),
            },
          ],
        }),
      );
    });
    const list = await screen.findByLabelText("Skills loaded by the run");
    expect(list.textContent).toContain("code-review");
    expect(list.textContent).toContain("vendored");
    expect(list.textContent).toContain("cccccccccccc");
  });

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

  it("offers only verified deployments and starts the run with the chosen one", async () => {
    const bodies: string[] = [];
    setup((url, init) => {
      if (url.endsWith("/agent/deployments"))
        return json({ default: "gpt-4-1", verified: ["gpt-5"] });
      if (init?.method === "POST") {
        bodies.push(String(init.body));
        return json(run({ status: "finished", deployment: "gpt-5" }), 202);
      }
      return url.endsWith("/agent-runs") ? json([]) : json(run());
    });
    const select = (await screen.findByLabelText("Model")) as HTMLSelectElement;
    expect(Array.from(select.options).map((o) => o.value)).toEqual([
      "",
      "gpt-5",
    ]);
    fireEvent.change(select, { target: { value: "gpt-5" } });
    fireEvent.click(screen.getByText("Run agent"));
    await act(async () => {
      await vi.advanceTimersByTimeAsync(10);
    });
    expect(JSON.parse(bodies[0]).deployment).toBe("gpt-5");
  });

  it("shows no model choice without verified deployments and omits the field", async () => {
    const bodies: string[] = [];
    setup((url, init) => {
      if (url.endsWith("/agent/deployments"))
        return json({ default: "gpt-4-1", verified: [] });
      if (init?.method === "POST") {
        bodies.push(String(init.body));
        return json(run({ status: "finished" }), 202);
      }
      return url.endsWith("/agent-runs") ? json([]) : json(run());
    });
    fireEvent.click(await screen.findByText("Run agent"));
    await act(async () => {
      await vi.advanceTimersByTimeAsync(10);
    });
    expect(screen.queryByLabelText("Model")).toBeNull();
    expect(JSON.parse(bodies[0])).not.toHaveProperty("deployment");
  });

  it("shows each call and the run totals, and names an exhausted budget", async () => {
    const withCalls = run({
      status: "failed",
      stop_reason: "reasoning_budget_exhausted",
      deployment: "gpt-5",
      usage: {
        calls: 1,
        prompt_tokens: 100,
        completion_tokens: 25000,
        reasoning_tokens: 25000,
        latency_ms: 2000,
        finish_reasons: { length: 1 },
      },
      steps: [
        {
          ...run().steps[0],
          call: {
            prompt_tokens: 100,
            completion_tokens: 25000,
            reasoning_tokens: 25000,
            finish_reason: "length",
            latency_ms: 2000,
          },
        },
      ],
    });
    setup((url, init) => {
      if (init?.method === "POST") return json(withCalls, 202);
      return url.endsWith("/agent-runs") ? json([withCalls]) : json(withCalls);
    });
    const usage = await screen.findByLabelText("Model usage of the run");
    expect(usage.textContent).toContain("completion 25000");
    expect(usage.textContent).toContain("gpt-5");
    expect(screen.getByLabelText("Model call of step 1").textContent).toContain(
      "25000 reasoning, length",
    );
    expect(screen.getByText(/reasoning budget exhausted/)).toBeTruthy();
  });

  it("shows the model per step, escalations, per-deployment totals and routing settings", async () => {
    const routed = run({
      status: "stopped",
      stop_reason: "heavy_call_limit",
      usage: {
        calls: 2,
        prompt_tokens: 200,
        completion_tokens: 50,
        reasoning_tokens: 0,
        latency_ms: 1000,
        finish_reasons: { stop: 2 },
        cost: 0.01,
        by_deployment: [
          {
            deployment: "light-m",
            calls: 1,
            heavy_calls: 0,
            prompt_tokens: 100,
            completion_tokens: 20,
            cost: 0,
          },
          {
            deployment: "heavy-m",
            calls: 1,
            heavy_calls: 1,
            prompt_tokens: 100,
            completion_tokens: 30,
            cost: 0.01,
          },
        ],
        escalations: [
          { step_no: 2, reason: "invalid_actions", deployment: "heavy-m" },
        ],
      },
      steps: [
        {
          ...run().steps[0],
          call: {
            prompt_tokens: 100,
            completion_tokens: 20,
            reasoning_tokens: 0,
            finish_reason: "stop",
            latency_ms: 500,
            deployment: "light-m",
            tier: "light",
            routing_reason: "policy",
          },
        },
      ],
    });
    setup((url, init) => {
      if (url.endsWith("/agent/routing"))
        return json({
          enabled: true,
          light: "light-m",
          heavy: "heavy-m",
          token_limit: 5000,
          heavy_call_limit: null,
          cost_limit: null,
          light_actions: ["file_selection"],
          heavy_actions: ["repair"],
          problems: [],
        });
      if (init?.method === "POST") return json(routed, 202);
      return url.endsWith("/agent-runs") ? json([routed]) : json(routed);
    });
    expect(
      (await screen.findByLabelText("Model call of step 1")).textContent,
    ).toContain("light-m (light, policy)");
    expect(screen.getByLabelText("Usage per deployment").textContent).toContain(
      "heavy-m: 1 calls (1 heavy)",
    );
    expect(
      screen.getByLabelText("Escalations of the run").textContent,
    ).toContain("two invalid actions in a row");
    expect(screen.getByText(/heavy-model call limit reached/)).toBeTruthy();
    const settings = await screen.findByLabelText("Model routing settings");
    expect(settings.textContent).toContain("tokens 5000");
    expect(settings.textContent).toContain("heavy calls off");
  });

  const CI_LIVE: CILive = {
    provider: "github_actions",
    approval: "pending",
    approved_by: null,
    keep_branches: false,
    phase: "awaiting_approval",
    command_id: "pytest",
    branch: "agent/ci/r1",
    workflow_run_id: null,
    workflow_url: null,
    conclusion: null,
    message: null,
  };

  it("asks once for the CI push and sends the user's answer", async () => {
    let live = CI_LIVE;
    const { fetchMock } = setup((url) => {
      if (url.endsWith("/ci/approval")) {
        live = {
          ...CI_LIVE,
          approval: "approved",
          approved_by: "Ivan",
          keep_branches: true,
          phase: "pushing",
        };
        return json(live);
      }
      if (url.endsWith("/agent-runs")) return json([run({ ci: live })]);
      return json(run({ ci: live }));
    });
    const group = await screen.findByRole("group", {
      name: "CI push approval",
    });
    expect(group.textContent).toContain("agent/ci/r1");
    const approve = within(group).getByRole("button", {
      name: "Approve CI push",
    });
    expect(approve).toHaveProperty("disabled", true); // a name is required
    fireEvent.change(within(group).getByLabelText(/Your name/), {
      target: { value: "Ivan" },
    });
    fireEvent.click(within(group).getByLabelText(/Keep the CI branches/));
    fireEvent.click(approve);
    expect(await screen.findByText(/push approved by Ivan/)).toBeTruthy();
    expect(
      screen.queryByRole("group", { name: "CI push approval" }),
    ).toBeNull();
    const sent = fetchMock.mock.calls.find(([u]) =>
      String(u).endsWith("/agent-runs/r1/ci/approval"),
    );
    expect(sent?.[1]?.method).toBe("POST");
    expect(JSON.parse(String(sent?.[1]?.body))).toEqual({
      approve: true,
      approved_by: "Ivan",
      keep_branches: true,
    });
  });

  it("denies the CI push", async () => {
    let live = CI_LIVE;
    const { fetchMock } = setup((url) => {
      if (url.endsWith("/ci/approval")) {
        live = { ...CI_LIVE, approval: "denied", phase: "denied" };
        return json(live);
      }
      if (url.endsWith("/agent-runs")) return json([run({ ci: live })]);
      return json(run({ ci: live }));
    });
    const group = await screen.findByRole("group", {
      name: "CI push approval",
    });
    fireEvent.change(within(group).getByLabelText(/Your name/), {
      target: { value: "Ivan" },
    });
    fireEvent.click(within(group).getByRole("button", { name: "Deny" }));
    expect(await screen.findByText(/push denied/)).toBeTruthy();
    const sent = fetchMock.mock.calls.find(([u]) =>
      String(u).endsWith("/ci/approval"),
    );
    expect(JSON.parse(String(sent?.[1]?.body))).toMatchObject({
      approve: false,
      keep_branches: false,
    });
  });

  it("shows the CI status, the workflow link and each CI job", async () => {
    const url = "https://github.com/acme/app/actions/runs/42";
    const job: AgentJob = {
      id: "j1",
      command_id: "pytest",
      variant: "changed",
      status: "failed",
      timeout_seconds: 900,
      exit_code: 1,
      duration_ms: 60000,
      truncated: false,
      select_test: null,
      flaky: null,
      ci: {
        provider: "github_actions",
        status: "completed",
        branch: "agent/ci/r1",
        commit: "abc",
        workflow_run_id: 42,
        workflow_url: url,
        conclusion: "failure",
        report: true,
        approved_by: "Ivan",
        kept: false,
        branch_deleted: true,
        error: null,
      },
    };
    const live: CILive = {
      ...CI_LIVE,
      approval: "approved",
      approved_by: "Ivan",
      phase: "in_progress",
      workflow_run_id: 42,
      workflow_url: url,
    };
    setup((u) => {
      if (u.endsWith("/agent-runs")) return json([run({ ci: live })]);
      return json(run({ ci: live, jobs: [job] }));
    });
    const status = await screen.findByLabelText("CI status");
    expect(status.textContent).toContain("in progress");
    expect(
      within(status).getByRole("link", { name: "workflow run" }),
    ).toHaveProperty("href", url);
    expect(
      screen.queryByRole("group", { name: "CI push approval" }),
    ).toBeNull();
    const info = screen.getByLabelText("CI of job j1");
    expect(info.textContent).toContain("CI completed (failure)");
    expect(info.textContent).toContain("agent/ci/r1");
    expect(info.textContent).toContain("branch deleted");
    expect(info.textContent).toContain("approved by Ivan");
  });

  it("never renders a non-https workflow link", async () => {
    const live: CILive = {
      ...CI_LIVE,
      approval: "approved",
      phase: "queued",
      workflow_url: "javascript:alert(1)",
    };
    setup((u) =>
      u.endsWith("/agent-runs")
        ? json([run({ ci: live })])
        : json(run({ ci: live })),
    );
    const status = await screen.findByLabelText("CI status");
    expect(within(status).queryByRole("link")).toBeNull();
  });
});

const LOOP: CiLoopView = {
  coding_step: "S-01",
  branch: "agent/ci/r1",
  approval: "pending",
  approved_by: null,
  approved_at: null,
  phase: "awaiting_approval",
  iteration: 0,
  max_iterations: 5,
  commit: null,
  workflow_status: null,
  workflow_url: null,
  baseline_url: null,
  counts: { new: 0, pre_existing: 0, fixed: 0, flaky: 0, elsewhere: 0 },
  actionable: [],
  elsewhere: [],
  flaky: [],
  green: false,
  terminal: null,
  branch_deleted: false,
  tokens_used: 120,
  token_budget: 150000,
  elapsed_seconds: 12.5,
  time_limit_seconds: 300,
  message: null,
};

const INFO: CiLoopApprovalInfo = {
  run_id: "r1",
  coding_step: "S-01",
  repository: "acme/app",
  remote: "https://github.com/acme/app",
  branch: "agent/ci/r1",
  base_branch: "main",
  workflow: "archpilot-tests.yml",
  command: "pytest",
  max_iterations: 5,
  protected_notice:
    "The base branch, the default branch, main and master can never be changed.",
  approved: false,
};

function failure(name: string, over: Partial<CiLoopResult> = {}): CiLoopResult {
  return {
    name,
    file: "tests/test_invoice.py",
    outcome: "failed",
    category: "AssertionError",
    message: "AssertionError: not paid",
    signature: `sig-${name}`,
    novelty: "new",
    scope: "changed",
    linked_via: null,
    ...over,
  };
}

describe("AgentRunPanel CI repair loop", () => {
  it("shows every approval detail and approves this run's branch only", async () => {
    const { fetchMock } = setup((u, init) => {
      if (u.endsWith("/ci-loop/approval-info")) return json(INFO);
      if (init?.method === "POST" && u.endsWith("/ci-loop/approval"))
        return json({ ...LOOP, approval: "approved", approved_by: "Ivan" });
      if (u.endsWith("/agent-runs")) return json([run({ ci_loop: LOOP })]);
      return json(run({ ci_loop: LOOP }));
    });
    const dialog = await screen.findByRole("group", {
      name: "CI loop approval",
    });
    const details = within(dialog).getByLabelText("CI loop approval details");
    for (const text of [
      "acme/app",
      "https://github.com/acme/app",
      "agent/ci/r1",
      "main",
      "archpilot-tests.yml",
      "5",
    ])
      expect(details.textContent).toContain(text);
    expect(within(dialog).getByRole("note").textContent).toMatch(
      /main and master can never be changed/,
    );
    const approve = within(dialog).getByRole("button", {
      name: "Approve CI branch agent/ci/r1",
    });
    expect((approve as HTMLButtonElement).disabled).toBe(true); // a name is required
    fireEvent.change(within(dialog).getByLabelText("Your name"), {
      target: { value: " Ivan " },
    });
    fireEvent.click(approve);
    expect(await screen.findByText(/Approval: approved by Ivan/)).toBeTruthy();
    const post = fetchMock.mock.calls.find(
      ([u, i]) =>
        String(u).endsWith("/ci-loop/approval") && i?.method === "POST",
    );
    expect(String(post?.[0])).toContain(
      "/api/projects/p/agent-runs/r1/ci-loop/approval",
    );
    expect(JSON.parse(String(post?.[1]?.body))).toEqual({
      approved_by: "Ivan",
    });
    expect(
      screen.queryByRole("group", { name: "CI loop approval" }),
    ).toBeNull();
  });

  it("denies a pending approval", async () => {
    const { fetchMock } = setup((u, init) => {
      if (u.endsWith("/ci-loop/approval-info")) return json(INFO);
      if (init?.method === "POST" && u.endsWith("/ci-loop/revoke"))
        return json({ ...LOOP, approval: "revoked" });
      if (u.endsWith("/agent-runs")) return json([run({ ci_loop: LOOP })]);
      return json(run({ ci_loop: LOOP }));
    });
    const dialog = await screen.findByRole("group", {
      name: "CI loop approval",
    });
    fireEvent.click(within(dialog).getByRole("button", { name: "Deny" }));
    expect(await screen.findByText(/Approval: revoked/)).toBeTruthy();
    expect(
      fetchMock.mock.calls.some(([u]) =>
        String(u).endsWith("/ci-loop/approval"),
      ),
    ).toBe(false);
  });

  it("shows the iteration, commit, workflow, counts, failures and budgets", async () => {
    const live: CiLoopView = {
      ...LOOP,
      approval: "approved",
      approved_by: "Ivan",
      phase: "polling",
      iteration: 2,
      commit: "c2d4e6f8a0b1c3d5",
      workflow_status: "in_progress",
      workflow_url: "https://github.com/acme/app/actions/runs/1002",
      counts: { new: 2, pre_existing: 1, fixed: 3, flaky: 1, elsewhere: 1 },
      actionable: [
        failure("tests.test_invoice::test_paid"),
        failure("tests.test_report::test_summary", {
          scope: "elsewhere",
          linked_via: "src/invoice.py",
          file: "tests/test_report.py",
        }),
      ],
      elsewhere: [
        failure("tests.test_other::test_unrelated", {
          scope: "elsewhere",
          file: "tests/test_other.py",
        }),
      ],
      flaky: [failure("tests.test_net::test_retry", { novelty: "flaky" })],
    };
    setup((u) =>
      u.endsWith("/agent-runs")
        ? json([run({ ci_loop: live })])
        : json(run({ ci_loop: live })),
    );
    const panel = await screen.findByLabelText("CI repair loop");
    expect(panel.textContent).toContain("Coding step S-01");
    expect(panel.textContent).toContain("agent/ci/r1");
    expect(
      within(panel).getByLabelText("CI loop approval status").textContent,
    ).toBe("Approval: approved by Ivan");
    const progress = within(panel).getByLabelText("CI loop progress");
    expect(progress.textContent).toContain("Iteration 2/5");
    expect(progress.textContent).toContain("c2d4e6f8a0b1");
    expect(progress.textContent).toContain("workflow in_progress");
    expect(
      within(progress)
        .getByRole("link", { name: "workflow run" })
        .getAttribute("href"),
    ).toBe("https://github.com/acme/app/actions/runs/1002");
    expect(within(panel).getByLabelText("CI loop counts").textContent).toBe(
      "2 new · 1 pre-existing · 3 fixed · 1 flaky · 1 elsewhere",
    );
    expect(within(panel).getByLabelText("CI loop budgets").textContent).toBe(
      "Tokens 120/150000 · time 12.5/300 s",
    );
    const actionable = within(panel).getByLabelText("Actionable failures");
    expect(actionable.textContent).toContain("tests.test_invoice::test_paid");
    expect(actionable.textContent).toContain(
      "linked to changed file src/invoice.py",
    );
    expect(
      within(panel).getByLabelText(/New failures elsewhere/).textContent,
    ).toContain("tests.test_other::test_unrelated");
    expect(within(panel).getByLabelText("Flaky tests").textContent).toContain(
      "tests.test_net::test_retry",
    );
    // Stop is offered while the run waits for CI; the approval can be revoked.
    expect(screen.getByRole("button", { name: "Stop" })).toBeTruthy();
    expect(
      within(panel).getByRole("button", { name: "Revoke CI approval" }),
    ).toBeTruthy();
    expect(within(panel).queryByRole("button", { name: /Resume/ })).toBeNull();
    expect(
      within(panel).queryByRole("button", { name: "Delete CI branch" }),
    ).toBeNull();
  });

  it("resumes a stopped run from its checkpoint and shows a refusal", async () => {
    const stopped: CiLoopView = {
      ...LOOP,
      approval: "revoked",
      approved_by: "Ivan",
      phase: "ended",
      iteration: 1,
      commit: "c1",
      terminal: "cancelled",
    };
    let refuse = true;
    const { fetchMock } = setup((u, init) => {
      if (init?.method === "POST" && u.endsWith("/steps/S-01/resume")) {
        if (refuse)
          return json(
            {
              detail: {
                code: "foreign_commits",
                message: "The CI branch has commits this run did not make",
                problems: ["foreign_commits"],
              },
            },
            409,
          );
        return json(run({ id: "r2", ci_loop: { ...LOOP, iteration: 1 } }), 202);
      }
      const ended = run({
        status: "stopped",
        stop_reason: "cancelled",
        ci_loop: stopped,
      });
      return u.endsWith("/agent-runs") ? json([ended]) : json(ended);
    });
    const resume = await screen.findByRole("button", { name: "Resume S-01" });
    fireEvent.click(resume);
    expect(
      await screen.findByText(/did not make \(foreign_commits\)/),
    ).toBeTruthy();
    refuse = false;
    fireEvent.click(screen.getByRole("button", { name: "Resume S-01" }));
    expect(await screen.findByText("running")).toBeTruthy();
    const posts = fetchMock.mock.calls.filter(([u]) =>
      String(u).endsWith("/plans/pl/tasks/T-001/checklist/steps/S-01/resume"),
    );
    expect(posts).toHaveLength(2);
  });

  it("offers branch deletion after the run, never automatically", async () => {
    const green: CiLoopView = {
      ...LOOP,
      approval: "approved",
      approved_by: "Ivan",
      phase: "ended",
      iteration: 2,
      commit: "c2",
      green: true,
      terminal: "green",
    };
    const { fetchMock } = setup((u, init) => {
      if (init?.method === "POST" && u.endsWith("/ci-loop/delete-branch")) {
        const body = JSON.parse(String(init.body)) as { confirm: boolean };
        if (!body.confirm)
          return json(
            {
              detail: {
                code: "confirm_required",
                message: "A patch references this branch; confirm the deletion",
              },
            },
            409,
          );
        return json({ ...green, branch_deleted: true });
      }
      const done = run({
        status: "finished",
        stop_reason: "green",
        patch_id: "pat1",
        ci_loop: green,
      });
      return u.endsWith("/agent-runs") ? json([done]) : json(done);
    });
    const panel = await screen.findByLabelText("CI repair loop");
    expect(within(panel).queryByRole("button", { name: /Resume/ })).toBeNull();
    expect(
      fetchMock.mock.calls.some(([u]) => String(u).endsWith("/delete-branch")),
    ).toBe(false);
    fireEvent.click(
      within(panel).getByRole("button", { name: "Delete CI branch" }),
    );
    expect(await screen.findByText(/confirm the deletion/)).toBeTruthy();
    fireEvent.click(
      within(panel).getByLabelText("Delete even if a patch uses it"),
    );
    fireEvent.click(
      within(panel).getByRole("button", { name: "Delete CI branch" }),
    );
    expect(await screen.findByText(/\(deleted\)/)).toBeTruthy();
    expect(
      within(panel).queryByRole("button", { name: "Delete CI branch" }),
    ).toBeNull();
  });

  it("has no CI loop block for a run without one", async () => {
    setup((u) => (u.endsWith("/agent-runs") ? json([run()]) : json(run())));
    expect(await screen.findByText("running")).toBeTruthy();
    expect(screen.queryByLabelText("CI repair loop")).toBeNull();
  });
});

describe("AgentRunPanel with advanced off", () => {
  beforeEach(() => window.localStorage.clear());

  it("renders no hooks, skills, routing or tier badges", async () => {
    const full = run({
      status: "finished",
      hooks: [],
      skills: [
        { name: "code-review", source: "vendored", version: "4ae2317", content_hash: "c".repeat(64) },
      ],
    });
    vi.stubGlobal(
      "fetch",
      vi.fn(async (url: string) => {
        if (url.endsWith("/agent/routing"))
          return json({ enabled: true, light: "l", heavy: "h", token_limit: 1, heavy_call_limit: null, cost_limit: null, light_actions: [], heavy_actions: [], problems: [] });
        if (url.endsWith("/agent-hooks")) return json([]);
        if (url.endsWith("/agent-skills")) return json({ skills: [], problems: [] });
        return url.endsWith("/agent-runs") ? json([full]) : json(full);
      }),
    );
    render(
      <AdvancedProvider>
        <AgentRunPanel projectId="p" planId="pl" task={TASK} acknowledge={false} disabled={false} onOpenPatch={vi.fn()} />
      </AdvancedProvider>,
    );
    expect(await screen.findByText("finished")).toBeTruthy();
    expect(screen.queryByLabelText("Skills loaded by the run")).toBeNull();
    expect(screen.queryByLabelText("Active hooks of the run")).toBeNull();
    expect(screen.queryByLabelText("Model routing settings")).toBeNull();
    expect(screen.queryByLabelText("Agent hooks")).toBeNull();
  });
});
