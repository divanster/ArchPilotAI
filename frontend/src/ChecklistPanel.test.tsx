import { act, fireEvent, render, screen, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import ChecklistPanel from "./ChecklistPanel";
import type {
  ChecklistStep,
  ChecklistView,
  PlanTask,
  TaskChecklist,
} from "./api";

const TASK = {
  key: "T-001",
  title: "Invoices",
  flags: [],
  blocking_questions: [],
} as unknown as PlanTask;

const BASE = "/plans/pl/tasks/T-001/checklist";

function step(key: string, over: Partial<ChecklistStep> = {}): ChecklistStep {
  return {
    key,
    title: `Title ${key}`,
    goal: `Goal ${key}`,
    acceptance_criteria: [`Criterion ${key}`],
    expected_files: [{ path: "src/invoice.py", source: "task" }],
    depends_on: [],
    related_items: [],
    grouping_reason:
      "Grouped: the criteria share the same component (Billing).",
    discovery_note: "",
    shared_criteria: [],
    criteria_count: 1,
    warnings: [],
    status: "todo",
    resumable: false,
    run: null,
    patch: null,
    checks: { tests: null, finding_total: null },
    ...over,
  };
}

function checklist(over: Partial<TaskChecklist> = {}): TaskChecklist {
  return {
    id: "c1",
    version: 1,
    status: "proposed",
    proposed_by: "system",
    proposal_reason: "Proposed from the task",
    decided_by: null,
    decision_reason: null,
    created_at: "2026-01-01T00:00:00Z",
    decided_at: null,
    task_key: "T-001",
    read_only: false,
    warnings: [],
    manual_verification: [],
    steps: [step("S-01"), step("S-02", { depends_on: ["S-01"] })],
    ...over,
  };
}

function view(
  c: TaskChecklist | null,
  next: string | null = null,
): ChecklistView {
  return {
    task_key: "T-001",
    checklist: c,
    next_step: next,
    history: c ? [c] : [],
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
  const onStatusChange = vi.fn();
  render(
    <ChecklistPanel
      projectId="p"
      planId="pl"
      task={TASK}
      acknowledge={false}
      disabled={false}
      onOpenPatch={onOpenPatch}
      onStatusChange={onStatusChange}
    />,
  );
  return { fetchMock, onOpenPatch, onStatusChange };
}

function calls(fetchMock: ReturnType<typeof vi.fn>, method: string) {
  return fetchMock.mock.calls.filter(
    ([, init]) =>
      ((init as RequestInit | undefined)?.method ?? "GET") === method,
  );
}

beforeEach(() => vi.useFakeTimers({ shouldAdvanceTime: true }));
afterEach(() => {
  vi.useRealTimers();
  vi.unstubAllGlobals();
});

describe("ChecklistPanel", () => {
  it("proposes a checklist and shows the steps in dependency order", async () => {
    let current = view(null);
    const { fetchMock } = setup((url, init) => {
      if (url.endsWith(BASE) && init?.method === "POST") {
        current = view(checklist());
        return json(current, 201);
      }
      return json(current);
    });
    fireEvent.click(
      await screen.findByRole("button", { name: "Propose checklist" }),
    );
    const list = await screen.findByRole("list", { name: "Checklist steps" });
    const items = within(list)
      .getAllByRole("listitem")
      .filter((li) => li.dataset.testid);
    expect(items.map((li) => li.dataset.testid)).toEqual([
      "step-S-01",
      "step-S-02",
    ]);
    expect(screen.getByLabelText("Depends on of S-02")).toHaveValue("S-01");
    expect(screen.getByText("Criterion S-01")).toBeInTheDocument();
    expect(calls(fetchMock, "POST")).toHaveLength(1);
    // Nothing can run before a person approves.
    expect(screen.queryByRole("button", { name: /Run next step/ })).toBeNull();
  });

  it("approves with a name and reason, then runs the next step", async () => {
    let current = view(checklist());
    const { fetchMock } = setup((url, init) => {
      if (url.endsWith(`${BASE}/approve`)) {
        expect(JSON.parse(String(init?.body))).toEqual({
          by: "ana",
          reason: "fine",
        });
        current = view(
          checklist({ status: "approved", read_only: true, decided_by: "ana" }),
          "S-01",
        );
        return json(current);
      }
      if (url.endsWith(`${BASE}/run-next`)) {
        expect(JSON.parse(String(init?.body))).toEqual({
          acknowledge_blocking_questions: false,
          restart: false,
        });
        current = view(
          checklist({
            status: "approved",
            read_only: true,
            steps: [
              step("S-01", {
                status: "proposed",
                patch: { id: "pa1", version: 1, status: "proposed" },
                checks: { tests: "passed", finding_total: 0 },
              }),
              step("S-02", { depends_on: ["S-01"] }),
            ],
          }),
          "S-01",
        );
        return json({ id: "r1", status: "running" }, 202);
      }
      return json(current);
    });
    const approve = await screen.findByRole("button", {
      name: "Approve checklist",
    });
    expect(approve).toBeDisabled();
    fireEvent.change(screen.getByLabelText(/Your name/), {
      target: { value: "ana" },
    });
    fireEvent.change(screen.getByLabelText(/^Reason/), {
      target: { value: "fine" },
    });
    fireEvent.click(approve);
    expect(
      await screen.findByText("This checklist version is read-only."),
    ).toBeInTheDocument();
    expect(
      screen.queryByRole("button", { name: "Approve checklist" }),
    ).toBeNull();
    fireEvent.click(
      await screen.findByRole("button", { name: "Run next step (S-01)" }),
    );
    const status = await screen.findByLabelText("Status of S-01");
    await vi.waitFor(() =>
      expect(status.textContent).toContain("patch v1 proposed"),
    );
    expect(status.textContent).toContain("tests passed");
    expect(status.textContent).toContain("0 finding(s)");
    expect(
      screen.getByRole("button", { name: "Run next step (S-01)" }),
    ).toBeDisabled();
    expect(calls(fetchMock, "POST")).toHaveLength(2);
  });

  it("offers resume and restart for a stopped step", async () => {
    const stopped = step("S-01", {
      status: "stopped",
      resumable: true,
      run: {
        id: "r1",
        status: "stopped",
        stop_reason: "user_stop",
        steps_taken: 2,
        resumed_from_run_id: null,
        started_at: "2026-01-01T00:00:00Z",
        finished_at: "2026-01-01T00:00:05Z",
      },
    });
    const current = view(
      checklist({ status: "approved", read_only: true, steps: [stopped] }),
      "S-01",
    );
    const { fetchMock } = setup(() => json(current));
    expect(await screen.findByLabelText("Status of S-01")).toHaveTextContent(
      "checkpoint saved",
    );
    fireEvent.click(screen.getByRole("button", { name: "Resume S-01" }));
    await vi.waitFor(() =>
      expect(
        fetchMock.mock.calls.some(([u]) =>
          String(u).endsWith(`${BASE}/steps/S-01/resume`),
        ),
      ).toBe(true),
    );
    await vi.waitFor(() =>
      expect(
        screen.getByRole("button", { name: "Restart S-01" }),
      ).not.toBeDisabled(),
    );
    fireEvent.click(screen.getByRole("button", { name: "Restart S-01" }));
    await vi.waitFor(() => {
      const restart = fetchMock.mock.calls.find(([u]) =>
        String(u).endsWith(`${BASE}/run-next`),
      );
      expect(restart).toBeDefined();
      expect(
        JSON.parse(String((restart?.[1] as RequestInit).body)),
      ).toMatchObject({
        restart: true,
      });
    });
  });

  it("shows validation problems of a rejected revision", async () => {
    const current = view(
      checklist({
        manual_verification: [
          { criterion: "Totals are checked by finance", reason: "By hand" },
        ],
      }),
    );
    const { fetchMock } = setup((_url, init) => {
      if (init?.method === "PUT") {
        return json(
          {
            detail: {
              code: "checklist_invalid",
              message:
                "The checklist is invalid: Dependency cycle: S-01 -> S-02 -> S-01",
              problems: [
                {
                  code: "dependency_cycle",
                  step: "S-01",
                  message: "Dependency cycle: S-01 -> S-02 -> S-01",
                },
              ],
            },
          },
          422,
        );
      }
      return json(current);
    });
    fireEvent.change(await screen.findByLabelText("Depends on of S-01"), {
      target: { value: "S-02" },
    });
    fireEvent.change(screen.getByLabelText(/Your name/), {
      target: { value: "ana" },
    });
    fireEvent.change(screen.getByLabelText(/^Reason/), {
      target: { value: "x" },
    });
    expect(
      screen.getByRole("button", { name: "Approve checklist" }),
    ).toBeDisabled();
    fireEvent.click(screen.getByRole("button", { name: "Save revision" }));
    const problems = await screen.findByRole("list", {
      name: "Checklist problems",
    });
    expect(problems).toHaveTextContent("dependency_cycle");
    const put = fetchMock.mock.calls.find(
      ([, init]) => (init as RequestInit | undefined)?.method === "PUT",
    );
    const body = JSON.parse(String((put?.[1] as RequestInit).body));
    expect(body.manual_verification).toEqual([
      { criterion: "Totals are checked by finance", reason: "By hand" },
    ]);
    expect(body.steps[0]).toMatchObject({
      key: "S-01",
      depends_on: ["S-02"],
      grouping_reason: expect.stringContaining("same component"),
      discovery_note: "",
      shared_criteria: [],
    });
  });

  it("shows cohesive steps, file discovery, warnings and the step-mode switch", async () => {
    const current = view(
      checklist({
        manual_verification: [
          { criterion: "Finance gives sign-off", reason: "A person checks" },
        ],
        steps: [
          step("S-01", {
            acceptance_criteria: ["A", "B", "C"],
            criteria_count: 3,
            expected_files: [],
            discovery_note:
              "No repository is connected: file discovery will happen during the run.",
          }),
          step("S-02", {
            warnings: [
              "S-02 expects 6 files (more than 5); consider splitting it.",
            ],
          }),
        ],
      }),
    );
    const { onStatusChange } = setup(() => json(current));
    expect(
      await screen.findByLabelText("Criteria count of S-01"),
    ).toHaveTextContent("3 criteria");
    expect(screen.getByLabelText("Why grouped S-01")).toHaveTextContent(
      "same component (Billing)",
    );
    const files = screen.getByLabelText("Expected files of S-01");
    expect(files).toHaveTextContent("discover during run");
    expect(files).toHaveTextContent("No repository is connected");
    expect(screen.getByLabelText("Expected files of S-02")).toHaveTextContent(
      "src/invoice.py",
    );
    expect(screen.getByLabelText("Warnings of S-02")).toHaveTextContent(
      "more than 5",
    );
    expect(screen.getByLabelText("Manual verification")).toHaveTextContent(
      "Finance gives sign-off",
    );
    expect(screen.getByRole("note")).toHaveTextContent(
      "Approving switches this task to step-based mode",
    );
    await vi.waitFor(() =>
      expect(onStatusChange).toHaveBeenLastCalledWith("proposed"),
    );
  });

  it("polls while a step runs and opens a step patch", async () => {
    let polls = 0;
    const running = view(
      checklist({
        status: "approved",
        read_only: true,
        steps: [
          step("S-01", {
            status: "running",
            patch: { id: "pa0", version: 1, status: "rejected" },
          }),
        ],
      }),
      "S-01",
    );
    const { onOpenPatch } = setup(() => {
      polls += 1;
      return json(running);
    });
    fireEvent.click(
      await screen.findByRole("button", { name: "Open patch of S-01" }),
    );
    expect(onOpenPatch).toHaveBeenCalledWith("pa0");
    const before = polls;
    await act(async () => {
      await vi.advanceTimersByTimeAsync(1600);
    });
    expect(polls).toBeGreaterThan(before);
  });
});
