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
    not_for_client: ["Why were no passages cited?"],
    dependency_count: 1,
    unmapped_node_count: 1,
    warnings: ["1 diagram node(s) have no component with the same name: Entra ID."],
    manual_edits: [],
    previous_version: null,
    previous_manual_edits: [],
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
        dependencies: [],
        flags: [],
        blocking_questions: ["Which IdP?"],
        notes: ["Unmapped dependency: depends on 'Entra ID', a diagram node with no component"],
        related: [],
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
        dependencies: [{ key: "T-001", source: "diagram", reason: null, author: null, created_at: null }],
        flags: ["AI recommendation: confirm technology before implementing"],
        blocking_questions: [],
        notes: [],
        related: ["T-001"],
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

function mockFetch(initial: ImplementationPlan[], post?: () => Response | ImplementationPlan) {
  const store = [...initial];
  const fetchMock = vi.fn(async (url: string, init?: RequestInit) => {
    const method = init?.method ?? "GET";
    if (url.endsWith("/requirements")) return json([{ id: RID, title: "Single sign-on", status: "confirmed" }]);
    if (url.endsWith("/architecture")) return json([]);
    if (method === "POST" && url.endsWith("/plans") && post) {
      const made = post();
      if (made instanceof Object && "ok" in made && !(made as Response).ok) return made as Response;
      const created = made as ImplementationPlan;
      store.unshift(created);
      return json(created, 201);
    }
    const edit = /\/plans\/([^/]+)\/dependencies(\/remove)?$/.exec(url);
    if (method === "POST" && edit) {
      const body = JSON.parse(String(init?.body));
      const target = store[0];
      const task = target.tasks.find((t) => t.id === body.task_id);
      const dep = target.tasks.find((t) => t.id === body.depends_on_task_id);
      if (!task || !dep) return json({ detail: "Both tasks must belong to this plan" }, 422);
      if (edit[2]) {
        task.depends_on = task.depends_on.filter((k) => k !== dep.key);
        task.dependencies = task.dependencies.filter((d) => d.key !== dep.key);
      } else {
        task.depends_on = [...task.depends_on, dep.key];
        task.dependencies = [
          ...task.dependencies,
          { key: dep.key, source: "manual", reason: body.reason, author: body.author, created_at: null },
        ];
      }
      return json(target);
    }
    const decision = /\/plans\/([^/]+)\/(approve|reject)$/.exec(url);
    if (method === "POST" && decision) {
      const target = store.find((p) => p.id === decision[1]);
      if (!target) return json({ detail: "not found" }, 404);
      target.status = decision[2] === "approve" ? "approved" : "rejected";
      return json(target);
    }
    if (url.endsWith("/plans")) return json(store.map(({ tasks, ...s }) => ({ ...s, task_count: tasks.length })));
    if (url.includes("/plans/")) return json(store[0]);
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
    expect(screen.getAllByText(/Unmapped dependency/, { selector: "span" }).length).toBeGreaterThan(0);
    expect(screen.getByText("Related: T-001")).toBeTruthy();
    expect(screen.getByText("Why were no passages cited?")).toBeTruthy();
    expect(screen.getByText("Not for the client")).toBeTruthy();
    expect(screen.getByText(/1 dependencies derived · 1 diagram node\(s\) unmapped/)).toBeTruthy();
    expect(screen.getByText(/have no component with the same name/)).toBeTruthy();
  });

  it("shows the unmapped banner on drafts only", async () => {
    mockFetch([plan()]);
    render(<PlanSection projectId={PID} />);
    expect(await screen.findByTestId("unmapped-banner")).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "Approve" }));
    await waitFor(() => expect(screen.queryByTestId("unmapped-banner")).toBeNull());
  });

  it("adds a dependency with a reason and marks it edited", async () => {
    mockFetch([plan()]);
    render(<PlanSection projectId={PID} />);
    await screen.findByText("T-001 · Implement API");
    fireEvent.click(screen.getAllByRole("button", { name: "Edit dependencies" })[0]);
    fireEvent.change(screen.getByLabelText("Dependency for T-001"), { target: { value: "t2" } });
    const add = screen.getAllByRole("button", { name: "Add" })[0] as HTMLButtonElement;
    expect(add.disabled).toBe(true);
    fireEvent.change(screen.getByLabelText("Reason for T-001"), { target: { value: "Shared schema" } });
    fireEvent.change(screen.getByLabelText("Author for T-001"), { target: { value: "ivan" } });
    fireEvent.click(add);
    const badge = await screen.findByText("edited");
    expect(badge.getAttribute("title")).toBe("Shared schema (ivan)");
  });

  it("shows derived nfr dependencies, no editor on reviewed plans, and previous manual edits", async () => {
    const base = plan();
    base.tasks[1].dependencies = [
      { key: "T-001", source: "nfr rule", reason: null, author: null, created_at: null },
    ];
    mockFetch([
      {
        ...base,
        status: "approved",
        version: 2,
        previous_version: 1,
        previous_manual_edits: [
          { action: "add", task_key: "T-002", depends_on_key: "T-001", reason: "Why", author: "ivan", created_at: "2026-01-01T00:00:00Z" },
        ],
      },
    ]);
    render(<PlanSection projectId={PID} />);
    expect(await screen.findByText("derived: nfr rule")).toBeTruthy();
    expect(screen.queryByText("Add dependency")).toBeNull();
    expect(screen.queryByRole("button", { name: "Edit dependencies" })).toBeNull();
    expect(screen.getByText(/Manual edits of version 1 \(not carried over\)/)).toBeTruthy();
    expect(screen.getByText(/Why \(ivan\)/)).toBeTruthy();
  });

  it("warns when a plan has zero dependencies", async () => {
    mockFetch([plan({ dependency_count: 0, unmapped_node_count: 0, warnings: ["No dependencies could be derived."] })]);
    render(<PlanSection projectId={PID} />);
    expect(await screen.findByText(/No dependencies could be derived/)).toBeTruthy();
  });

  it("refreshes the Plan step after build, approve and reject", async () => {
    let n = 0;
    mockFetch([], () => plan({ version: ++n, id: `pl${n}` }));
    const step = () => screen.getByText("Plan", { selector: "b" }).closest("li")?.textContent ?? "";
    render(<PlanSection projectId={PID} />);
    await waitFor(() => expect(step()).toMatch(/not built/));
    fireEvent.click(await screen.findByRole("button", { name: "Build plan" }));
    await waitFor(() => expect(step()).toMatch(/V1 draft/));
    fireEvent.click(screen.getByRole("button", { name: "Approve" }));
    await waitFor(() => expect(step()).toMatch(/V1 approved/));

    fireEvent.click(screen.getByRole("button", { name: "Rebuild plan" }));
    await waitFor(() => expect(screen.getByTestId("plan-status").textContent).toMatch(/draft/i));
    fireEvent.click(screen.getByRole("button", { name: "Reject" }));
    await waitFor(() => expect(screen.getByTestId("plan-status").textContent).toMatch(/rejected/i));
    expect(step()).toMatch(/V1 approved/); // an earlier approved version still counts
  });

  it("shows a rejected state on the Plan step when no version is approved", async () => {
    const step = () => screen.getByText("Plan", { selector: "b" }).closest("li")?.textContent ?? "";
    mockFetch([plan()]);
    render(<PlanSection projectId={PID} />);
    await waitFor(() => expect(step()).toMatch(/V1 draft/));
    fireEvent.click(screen.getByRole("button", { name: "Reject" }));
    await waitFor(() => expect(step()).toMatch(/V1 rejected/));
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

  it("hides the dependency form, opens one at a time, and Escape restores focus", async () => {
    mockFetch([plan()]);
    render(<PlanSection projectId={PID} />);
    await screen.findByText("T-001 · Implement API");
    expect(screen.queryByLabelText("Dependency for T-001")).toBeNull();
    const buttons = screen.getAllByRole("button", { name: "Edit dependencies" });
    expect(buttons).toHaveLength(2);
    fireEvent.click(buttons[0]);
    expect(screen.getByLabelText("Dependency for T-001")).toBeTruthy();
    fireEvent.click(buttons[1]);
    expect(screen.queryByLabelText("Dependency for T-001")).toBeNull();
    expect(screen.getByLabelText("Dependency for T-002")).toBeTruthy();
    fireEvent.keyDown(screen.getByLabelText("Reason for T-002"), { key: "Escape" });
    expect(screen.queryByLabelText("Dependency for T-002")).toBeNull();
    expect(document.activeElement).toBe(buttons[1]);
  });

  it("disables cycle-creating candidates and remembers reason across opens", async () => {
    mockFetch([plan()]);
    render(<PlanSection projectId={PID} />);
    await screen.findByText("T-001 · Implement API");
    const buttons = screen.getAllByRole("button", { name: "Edit dependencies" });
    fireEvent.click(buttons[0]);
    const option = screen.getByRole("option", { name: /T-002/ }) as HTMLOptionElement;
    expect(option.disabled).toBe(true);
    expect(option.title).toBe("Would create a cycle");
    fireEvent.change(screen.getByLabelText("Reason for T-001"), { target: { value: "Because" } });
    fireEvent.click(screen.getByRole("button", { name: "Cancel" }));
    fireEvent.click(buttons[0]);
    expect((screen.getByLabelText("Reason for T-001") as HTMLInputElement).value).toBe("Because");
  });

  it("asks for confirmation before removing a derived dependency", async () => {
    mockFetch([plan()]);
    render(<PlanSection projectId={PID} />);
    await screen.findByText("T-001 · Implement API");
    fireEvent.click(screen.getAllByRole("button", { name: "Edit dependencies" })[1]);
    fireEvent.change(screen.getByLabelText("Reason for T-002"), { target: { value: "r" } });
    fireEvent.change(screen.getByLabelText("Author for T-002"), { target: { value: "a" } });
    fireEvent.click(screen.getByRole("button", { name: /Remove derived dependency/ }));
    expect(screen.getByText(/next Rebuild will restore T-001/)).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "Confirm removal" }));
    await waitFor(() => expect(screen.queryByText("T-001", { selector: "span" })).toBeNull());
  });

  it("collapses older drafts but keeps approved versions visible", async () => {
    const versions = [6, 5, 4, 3, 2, 1].map((v) =>
      plan({ id: `pl${v}`, version: v, status: v === 2 ? "approved" : "draft" }),
    );
    mockFetch(versions);
    render(<PlanSection projectId={PID} />);
    await screen.findByText("T-001 · Implement API");
    const list = screen.getByRole("list", { name: "Plan versions" });
    const labels = () => Array.from(list.querySelectorAll("button.tab")).map((b) => b.textContent);
    expect(labels().some((l) => l?.startsWith("V2"))).toBe(true);
    expect(labels().some((l) => l?.startsWith("V1"))).toBe(false);
    fireEvent.click(screen.getByRole("button", { name: "Show 2 older drafts" }));
    expect(labels().some((l) => l?.startsWith("V1"))).toBe(true);
  });

  it("collapses requirements and criteria by default with counts, and toggles all", async () => {
    mockFetch([plan()]);
    render(<PlanSection projectId={PID} />);
    await screen.findByText("T-001 · Implement API");
    const summary = screen.getByText("Linked requirements (1)");
    const details = summary.closest("details") as HTMLDetailsElement;
    expect(details.open).toBe(false);
    fireEvent.click(screen.getByRole("button", { name: "Expand all" }));
    expect(details.open).toBe(true);
    fireEvent.click(screen.getByRole("button", { name: "Collapse all" }));
    expect(details.open).toBe(false);
  });

  it("trims a trailing numbered heading from the description only on match", async () => {
    mockFetch([plan()]);
    const { unmount } = render(
      <PlanSection projectId={PID} projectDescription={"Build a portal.\n2. Business objectives"} />,
    );
    await screen.findByText("T-001 · Implement API");
    expect(screen.getByText("Build a portal.")).toBeTruthy();
    expect(screen.queryByText(/Business objectives/)).toBeNull();
    unmount();
    render(<PlanSection projectId={PID} projectDescription={"Build a portal.\n2. business objectives"} />);
    expect(await screen.findByText(/2\. business objectives/)).toBeTruthy();
  });
});

