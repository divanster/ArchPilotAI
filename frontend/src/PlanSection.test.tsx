import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import PlanSection from "./PlanSection";
import type { ImplementationPlan } from "./api";

const PID = "11111111-1111-1111-1111-111111111111";
const RID = "33333333-3333-3333-3333-333333333333";

function plan(over: Partial<ImplementationPlan> = {}): ImplementationPlan {
  return {
    id: "pl1",
    project_id: PID,
    proposal_id: "p1",
    version: 1,
    status: "draft",
    generated_at: "2026-01-01T00:00:00Z",
    source_graph_built_at: "2026-01-01T00:00:00Z",
    task_count: 2,
    unattached_questions: ["Which region?"],
    tasks: [
      {
        id: "t1",
        key: "T-001",
        title: "Implement API",
        kind: "component",
        description: "",
        source_ref: "components[1]",
        component_ref: "api",
        requirement_ids: [RID],
        acceptance_criteria: ["Login works (needs clarification; gap: no IdP)"],
        depends_on: [],
        flags: [],
        blocking_questions: ["Which IdP?"],
        order_index: 1,
        status: "todo",
      },
      {
        id: "t2",
        key: "T-002",
        title: "Implement Web",
        kind: "component",
        description: "",
        source_ref: "components[0]",
        component_ref: "web",
        requirement_ids: [],
        acceptance_criteria: [],
        depends_on: ["T-001"],
        flags: ["AI recommendation: confirm technology before implementing"],
        blocking_questions: [],
        order_index: 2,
        status: "todo",
      },
    ],
    ...over,
  };
}

function json(body: unknown, status = 200) {
  return { ok: status < 400, status, json: async () => body } as Response;
}

function mockFetch(plans: ImplementationPlan[], post?: () => Response) {
  const fetchMock = vi.fn(async (url: string, init?: RequestInit) => {
    const method = init?.method ?? "GET";
    if (url.endsWith("/requirements")) return json([{ id: RID, title: "Single sign-on", status: "confirmed" }]);
    if (url.endsWith("/architecture")) return json([]);
    if (method === "POST" && url.endsWith("/plans") && post) return post();
    if (method === "POST" && url.endsWith("/approve")) return json({ ...plans[0], status: "approved" });
    if (url.endsWith("/plans")) return json(plans.map(({ tasks, ...s }) => ({ ...s, task_count: tasks.length })));
    if (url.includes("/plans/")) return json(plans[0]);
    return json([]);
  });
  vi.stubGlobal("fetch", fetchMock);
  return fetchMock;
}

describe("PlanSection", () => {
  beforeEach(() => vi.unstubAllGlobals());
  afterEach(() => vi.unstubAllGlobals());

  it("renders tasks in order with requirements, criteria, flags and questions", async () => {
    mockFetch([plan()]);
    render(<PlanSection projectId={PID} />);
    expect(await screen.findByText("T-001 · Implement API")).toBeTruthy();
    const order = screen.getAllByRole("heading", { level: 3 }).map((h) => h.textContent);
    expect(order.indexOf("T-001 · Implement API")).toBeLessThan(order.indexOf("T-002 · Implement Web"));
    expect(screen.getByText("Single sign-on")).toBeTruthy();
    expect(screen.getByText(/gap: no IdP/)).toBeTruthy();
    expect(screen.getByText("Which IdP?")).toBeTruthy();
    expect(screen.getByText("Which region?")).toBeTruthy();
    expect(screen.getByText(/AI recommendation: confirm technology/)).toBeTruthy();
    expect(screen.getByText("T-001", { selector: "p" })).toBeTruthy();
  });

  it("approves a draft plan", async () => {
    mockFetch([plan()]);
    render(<PlanSection projectId={PID} />);
    await screen.findByText("T-001 · Implement API");
    fireEvent.click(screen.getByRole("button", { name: "Approve" }));
    await waitFor(() => expect(screen.getByTestId("plan-status").textContent).toMatch(/approved/i));
    expect((screen.getByRole("button", { name: "Approve" }) as HTMLButtonElement).disabled).toBe(true);
  });

  it("shows the server error when no plan can be built", async () => {
    mockFetch([], () => json({ detail: "No approved architecture" }, 409));
    render(<PlanSection projectId={PID} />);
    fireEvent.click(await screen.findByRole("button", { name: "Build plan" }));
    expect(await screen.findByText(/No approved architecture/)).toBeTruthy();
  });
});
