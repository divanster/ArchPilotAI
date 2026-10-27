import {
  fireEvent,
  render,
  screen,
  waitFor,
  within,
} from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import ArchitectureDiagram from "./ArchitectureDiagram";
import ArchitectureSection from "./ArchitectureSection";
import type { ArchitectureProposal, Decision } from "./api";

const PID = "11111111-1111-1111-1111-111111111111";
const RID = "33333333-3333-3333-3333-333333333333";

const decision: Decision = {
  name: "Auth",
  decision: "Use OIDC login.",
  rationale: "Client needs SSO.",
  origin: "required_by_client",
  supporting_requirement_ids: [RID],
  alternatives_considered: ["Basic auth"],
  assumptions: [],
  risks: [],
};

function proposal(over: Partial<ArchitectureProposal> = {}): ArchitectureProposal {
  return {
    id: "p1",
    project_id: PID,
    title: "Proposed architecture",
    summary: "A summary.",
    status: "draft",
    version: 1,
    provider: "fake",
    model: "fake-model",
    prompt_version: "architecture-v1",
    latency_ms: 1,
    input_tokens: 1,
    output_tokens: 1,
    created_at: "2026-01-01T00:00:00Z",
    updated_at: "2026-01-01T00:00:00Z",
    stale_requirement_ids: [],
    content: {
      solution_overview: "Overview text",
      recommended_stack: [
        { ...decision, name: "Web", layer: "Frontend", technology: "React" },
      ],
      components: [
        {
          id: "web",
          name: "Web App",
          type: "client",
          responsibility: "UI",
          technology: null,
          requirement_ids: [RID],
        },
      ],
      integrations: [],
      data_architecture: [],
      security_architecture: [decision],
      non_functional_design: [],
      risks: [
        { description: "Vendor risk", severity: "high", mitigation: "Abstract it", requirement_ids: [] },
      ],
      assumptions: [{ statement: "Single region", requirement_ids: [] }],
      open_questions: [{ question: "Which IdP?", why_it_matters: null, requirement_ids: [] }],
      alternatives: [],
      implementation_phases: [{ name: "Phase 1", objectives: ["Build MVP"], requirement_ids: [] }],
      diagram: {
        nodes: [
          { id: "web", name: "Web App", type: "client" },
          { id: "api", name: "API", type: "api" },
        ],
        edges: [{ source: "web", target: "api", relationship: "calls" }],
      },
    },
    ...over,
  };
}

function json(body: unknown, status = 200) {
  return { ok: status < 400, status, json: async () => body } as Response;
}

const reqs = [{ id: RID, title: "Single sign-on" }];

describe("ArchitectureSection", () => {
  const fetchMock = vi.fn();
  beforeEach(() => vi.stubGlobal("fetch", fetchMock));
  afterEach(() => {
    fetchMock.mockReset();
    vi.unstubAllGlobals();
  });

  function route(list: ArchitectureProposal[], extra?: (url: string, init?: RequestInit) => Response | undefined) {
    fetchMock.mockImplementation(async (url: string, init?: RequestInit) => {
      const custom = extra?.(url, init);
      if (custom) return custom;
      if (url.endsWith("/requirements")) return json(reqs);
      if (url.endsWith("/architecture")) return json(list);
      const match = list.find((p) => url.endsWith(`/architecture/${p.id}`));
      if (match) return json(match);
      return json({ detail: "nope" }, 404);
    });
  }

  it("shows empty state", async () => {
    route([]);
    render(<ArchitectureSection projectId={PID} />);
    expect(await screen.findByText("No architecture proposals yet.")).toBeInTheDocument();
  });

  it("shows a load error with retry", async () => {
    fetchMock.mockResolvedValue(json({ detail: "boom" }, 500));
    render(<ArchitectureSection projectId={PID} />);
    expect(await screen.findByRole("button", { name: "Retry architecture" })).toBeInTheDocument();
  });

  it("displays the proposal, origins, requirement titles and diagram", async () => {
    route([proposal()]);
    render(<ArchitectureSection projectId={PID} />);
    expect(await screen.findByText("Overview text")).toBeInTheDocument();
    expect(screen.getByTestId("proposal-status")).toHaveTextContent("draft");
    expect(screen.getAllByText("Required by client").length).toBeGreaterThan(0);
    expect(screen.getAllByText("Single sign-on").length).toBeGreaterThan(0);
    expect(screen.getByText("Frontend: React")).toBeInTheDocument();
    expect(screen.getByText("Vendor risk")).toBeInTheDocument();
    expect(screen.getByText("Which IdP?")).toBeInTheDocument();
    expect(screen.getByText("Build MVP")).toBeInTheDocument();
    expect(screen.getAllByTestId("diagram-node")).toHaveLength(2);
    expect(screen.getAllByTestId("diagram-edge")).toHaveLength(1);
  });

  it("approves a draft", async () => {
    const p = proposal();
    route([p], (_url, init) =>
      init?.method === "PATCH" ? json({ ...p, status: "approved" }) : undefined,
    );
    render(<ArchitectureSection projectId={PID} />);
    fireEvent.click(await screen.findByRole("button", { name: "Approve" }));
    await waitFor(() =>
      expect(screen.getByTestId("proposal-status")).toHaveTextContent("approved"),
    );
    expect(screen.getByRole("button", { name: "Approve" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "Reject" })).toBeDisabled();
    const patch = fetchMock.mock.calls.find((c) => c[1]?.method === "PATCH");
    expect(JSON.parse(patch![1].body)).toEqual({ status: "approved" });
  });

  it("rejects a draft", async () => {
    const p = proposal();
    route([p], (_url, init) =>
      init?.method === "PATCH" ? json({ ...p, status: "rejected" }) : undefined,
    );
    render(<ArchitectureSection projectId={PID} />);
    fireEvent.click(await screen.findByRole("button", { name: "Reject" }));
    await waitFor(() =>
      expect(screen.getByTestId("proposal-status")).toHaveTextContent("rejected"),
    );
  });

  it("edits title and summary only", async () => {
    const p = proposal();
    route([p], (_url, init) =>
      init?.method === "PATCH"
        ? json({ ...p, title: "New title" })
        : undefined,
    );
    render(<ArchitectureSection projectId={PID} />);
    fireEvent.click(await screen.findByRole("button", { name: "Edit" }));
    fireEvent.change(screen.getByLabelText("Proposal title"), { target: { value: "New title" } });
    fireEvent.click(screen.getByRole("button", { name: "Save proposal" }));
    expect(await screen.findByRole("heading", { name: "New title" })).toBeInTheDocument();
    const patch = fetchMock.mock.calls.find((c) => c[1]?.method === "PATCH");
    expect(JSON.parse(patch![1].body)).toEqual({ title: "New title" });
  });

  it("validates empty edit fields", async () => {
    route([proposal()]);
    render(<ArchitectureSection projectId={PID} />);
    fireEvent.click(await screen.findByRole("button", { name: "Edit" }));
    fireEvent.change(screen.getByLabelText("Proposal summary"), { target: { value: " " } });
    fireEvent.click(screen.getByRole("button", { name: "Save proposal" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("required");
  });

  it("shows API errors from review actions", async () => {
    route([proposal()], (_url, init) =>
      init?.method === "PATCH" ? json({ detail: "Conflict" }, 409) : undefined,
    );
    render(<ArchitectureSection projectId={PID} />);
    fireEvent.click(await screen.findByRole("button", { name: "Approve" }));
    expect(await screen.findByRole("alert")).toBeInTheDocument();
  });

  it("blocks approval when evidence is stale", async () => {
    route([proposal({ stale_requirement_ids: [RID] })]);
    render(<ArchitectureSection projectId={PID} />);
    expect(await screen.findByText(/no longer confirmed/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Approve" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "Reject" })).toBeEnabled();
  });

  it("generates with a loading state and prepends the new version", async () => {
    let finish: (r: Response) => void = () => {};
    const created = proposal({ id: "p2", version: 1 });
    route([], (url, init) =>
      url.endsWith("/architecture/generate") && init?.method === "POST"
        ? undefined
        : undefined,
    );
    const base = fetchMock.getMockImplementation()!;
    fetchMock.mockImplementation((url: string, init?: RequestInit) => {
      if (url.endsWith("/generate"))
        return new Promise<Response>((res) => (finish = res));
      return base(url, init);
    });
    render(<ArchitectureSection projectId={PID} />);
    await screen.findByText("No architecture proposals yet.");
    fireEvent.click(screen.getByRole("button", { name: "Generate architecture" }));
    expect(screen.getByRole("button", { name: "Generating…" })).toBeDisabled();
    finish(json(created, 201));
    expect(await screen.findByText("Overview text")).toBeInTheDocument();
    expect(
      within(screen.getByRole("list", { name: "Proposal versions" })).getByText(/Version 1/),
    ).toBeInTheDocument();
  });

  it("shows a generation error", async () => {
    route([], (_url, init) =>
      init?.method === "POST"
        ? json({ detail: "No confirmed requirements" }, 409)
        : undefined,
    );
    render(<ArchitectureSection projectId={PID} />);
    await screen.findByText("No architecture proposals yet.");
    fireEvent.click(screen.getByRole("button", { name: "Generate architecture" }));
    expect(await screen.findByRole("alert")).toHaveTextContent(/confirmed requirements/i);
    expect(screen.getByRole("button", { name: "Generate architecture" })).toBeEnabled();
  });
});

describe("ArchitectureDiagram", () => {
  it("renders nodes and edge labels as text, ignoring unknown edge targets", () => {
    const { container } = render(
      <ArchitectureDiagram
        nodes={[{ id: "a", name: "<img src=x onerror=alert(1)>", type: "api" }]}
        edges={[{ source: "a", target: "ghost", relationship: "calls" }]}
      />,
    );
    expect(screen.getAllByTestId("diagram-node")).toHaveLength(1);
    expect(screen.queryAllByTestId("diagram-edge")).toHaveLength(0);
    expect(container.querySelector("img")).toBeNull();
    expect(screen.getByText("<img src=x onerror=alert(1)>")).toBeInTheDocument();
  });

  it("shows an empty message without nodes", () => {
    render(<ArchitectureDiagram nodes={[]} edges={[]} />);
    expect(screen.getByText(/no diagram nodes/)).toBeInTheDocument();
  });

  it("tolerates cycles", () => {
    render(
      <ArchitectureDiagram
        nodes={[
          { id: "a", name: "A", type: "api" },
          { id: "b", name: "B", type: "api" },
        ]}
        edges={[
          { source: "a", target: "b", relationship: "x" },
          { source: "b", target: "a", relationship: "y" },
        ]}
      />,
    );
    expect(screen.getAllByTestId("diagram-edge")).toHaveLength(2);
  });
});
