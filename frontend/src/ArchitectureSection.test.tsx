import {
  fireEvent,
  render,
  screen,
  waitFor,
  within,
} from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import ArchitectureDiagram, { overlaps } from "./ArchitectureDiagram";
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

describe("coverage matrix", () => {
  const fetchMock = vi.fn();
  beforeEach(() => vi.stubGlobal("fetch", fetchMock));
  afterEach(() => {
    fetchMock.mockReset();
    vi.unstubAllGlobals();
  });

  function serve(p: ArchitectureProposal) {
    fetchMock.mockImplementation(async (url: string) => {
      if (url.endsWith("/requirements")) return json(reqs);
      if (url.endsWith("/architecture")) return json([p]);
      return json(p);
    });
  }

  it("shows 'Coverage not recorded' for a proposal without a matrix, never 'Fully covered'", async () => {
    const legacy = proposal({ prompt_version: "architecture-v3" });
    delete legacy.content.coverage_matrix;
    serve(legacy);
    render(<ArchitectureSection projectId={PID} />);
    expect(await screen.findByTestId("coverage-not-recorded")).toHaveTextContent(
      "Coverage not recorded",
    );
    expect(screen.queryByText(/fully covered/i)).toBeNull();
    expect(screen.queryAllByTestId("coverage-row")).toHaveLength(0);
    // Review actions still work for such proposals.
    expect(screen.getByRole("button", { name: "Approve" })).toBeEnabled();
    expect(screen.getByRole("button", { name: "Reject" })).toBeEnabled();
  });

  it("treats an explicit null matrix the same way", async () => {
    const legacy = proposal();
    legacy.content.coverage_matrix = null;
    serve(legacy);
    render(<ArchitectureSection projectId={PID} />);
    expect(await screen.findByText("Coverage not recorded")).toBeInTheDocument();
  });

  it("lists each requirement with status, verification and gap, and asks for human review", async () => {
    const p = proposal();
    p.content.coverage_matrix = [
      {
        requirement_id: RID,
        related_ids: ["web", "d-auth"],
        verification: "Login test with the client IdP",
        status: "partial",
        gap: "IdP not chosen",
      },
    ];
    p.content.security_architecture = [{ ...decision, id: "d-auth" }];
    serve(p);
    render(<ArchitectureSection projectId={PID} />);
    const row = await screen.findByTestId("coverage-row");
    expect(within(row).getByText("Single sign-on")).toBeInTheDocument();
    expect(within(row).getByText("Web App, Auth")).toBeInTheDocument();
    expect(within(row).getByText("Login test with the client IdP")).toBeInTheDocument();
    expect(within(row).getByText("Partial")).toBeInTheDocument();
    expect(within(row).getByText("IdP not chosen")).toBeInTheDocument();
    expect(screen.getByTestId("coverage-summary")).toHaveTextContent(
      "0 addressed, 1 partial, 0 unresolved",
    );
    expect(screen.getByTestId("coverage-summary")).toHaveTextContent(/person must review/);
  });

  it("labels client preferences distinctly from requirements", async () => {
    const p = proposal();
    p.content.data_architecture = [
      { ...decision, name: "Database", origin: "preferred_by_client" },
    ];
    serve(p);
    render(<ArchitectureSection projectId={PID} />);
    expect(await screen.findByText("Client preference")).toBeInTheDocument();
  });
});

describe("cited passages", () => {
  const fetchMock = vi.fn();
  beforeEach(() => vi.stubGlobal("fetch", fetchMock));
  afterEach(() => {
    fetchMock.mockReset();
    vi.unstubAllGlobals();
  });

  const CHUNK = "55555555-5555-5555-5555-555555555555";
  const DOC = "66666666-6666-6666-6666-666666666666";

  function serve(p: ArchitectureProposal) {
    fetchMock.mockImplementation(async (url: string) => {
      if (url.endsWith("/requirements")) return json(reqs);
      if (url.endsWith("/architecture")) return json([p]);
      if (url.endsWith(`/architecture/${p.id}`)) return json(p);
      return json({ detail: "nope" }, 404);
    });
  }

  function cited(): ArchitectureProposal {
    const base = proposal();
    base.content.security_architecture = [{ ...decision, source_passage_ids: [CHUNK] }];
    base.content.source_passages = [
      { chunk_id: CHUNK, document_id: DOC, filename: "<b>notes</b>.md", source_start: 10, source_end: 42 },
    ];
    base.content.retrieval = {
      status: "used",
      passages_provided: 1,
      unindexed_documents: 0,
      budget_chars: 6000,
      truncated: false,
    };
    return base;
  }

  it("shows the filename and offsets with a link to the document preview, as plain text", async () => {
    serve(cited());
    render(<ArchitectureSection projectId={PID} />);
    const link = await screen.findByRole("link", { name: /notes.*\[10–42\]/ });
    expect(link.getAttribute("href")).toBe(`#/documents/${PID}/${DOC}`);
    expect(link.textContent).toContain("<b>notes</b>.md");
    expect(link.querySelector("b")).toBeNull();
    expect(screen.getByTestId("retrieval-note").textContent).toMatch(
      /evidence, not requirements/,
    );
  });

  it("explains when documents were not indexed", async () => {
    const p = proposal();
    p.content.retrieval = {
      status: "not_indexed",
      passages_provided: 0,
      unindexed_documents: 2,
      budget_chars: 6000,
      truncated: false,
    };
    serve(p);
    render(<ArchitectureSection projectId={PID} />);
    expect((await screen.findByTestId("retrieval-note")).textContent).toMatch(
      /not indexed/,
    );
    expect(screen.queryByTestId("cited-passage")).toBeNull();
  });

  it("displays an old proposal unchanged, without claiming retrieval was used", async () => {
    serve(proposal());
    render(<ArchitectureSection projectId={PID} />);
    expect((await screen.findByTestId("retrieval-note")).textContent).toBe(
      "Document context not recorded",
    );
    expect(screen.queryByTestId("cited-passage")).toBeNull();
    expect(screen.getByText("Overview text")).toBeTruthy();
  });

  it("does not invent a source for an unknown citation", async () => {
    const p = cited();
    p.content.source_passages = [];
    serve(p);
    render(<ArchitectureSection projectId={PID} />);
    expect(await screen.findByText("Source unavailable")).toBeTruthy();
    expect(screen.queryByRole("link", { name: /notes/ })).toBeNull();
  });
});
describe("ArchitectureDiagram", () => {
  it("keeps edge labels apart from each other and from nodes", () => {
    const nodes = ["hub", "a", "b", "c", "d"].map((id) => ({
      id,
      name: id,
      type: "service" as const,
    }));
    const edges = [
      ...["a", "b", "c", "d"].map((t) => ({
        source: "hub",
        target: t,
        relationship: "reads and writes",
      })),
      { source: "a", target: "b", relationship: "notifies" },
      { source: "b", target: "a", relationship: "acknowledges" },
    ];
    const { container } = render(<ArchitectureDiagram nodes={nodes} edges={edges} />);
    const box = (el: Element) => ({
      x: Number(el.getAttribute("x")),
      y: Number(el.getAttribute("y")),
      w: Number(el.getAttribute("width")),
      h: Number(el.getAttribute("height")),
    });
    const labels = [...container.querySelectorAll(".edge-label-bg")].map(box);
    const nodeBoxes = [...container.querySelectorAll(".diagram-node rect")].map(box);
    expect(labels).toHaveLength(edges.length);
    labels.forEach((l, i) => {
      nodeBoxes.forEach((n) => expect(overlaps(l, n, 0)).toBe(false));
      labels.slice(i + 1).forEach((o) => expect(overlaps(l, o, 0)).toBe(false));
    });
  });

  it("draws arrowheads at the target and keeps the textual relationship list", () => {
    const { container } = render(
      <ArchitectureDiagram
        nodes={[
          { id: "web", name: "Web", type: "client" },
          { id: "api", name: "API", type: "api" },
        ]}
        edges={[{ source: "web", target: "api", relationship: "calls" }]}
      />,
    );
    const line = container.querySelector(".diagram-edge line")!;
    expect(line.getAttribute("marker-end")).toMatch(/^url\(#arrow-/);
    const markerId = line.getAttribute("marker-end")!.slice(5, -1);
    expect(container.querySelector(`marker[id="${markerId}"]`)).not.toBeNull();
    // Source sits above the target, so the arrow must point downwards.
    expect(Number(line.getAttribute("y2"))).toBeGreaterThan(Number(line.getAttribute("y1")));
    const list = screen.getByRole("list", { name: "Diagram relationships" });
    expect(within(list).getByText("Web calls API")).toBeInTheDocument();
  });

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
