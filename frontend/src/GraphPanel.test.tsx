import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import GraphPanel from "./GraphPanel";
import type { GraphData } from "./api";

const PID = "11111111-1111-1111-1111-111111111111";
const PROP = "p1";

const built: GraphData = {
  built: true,
  built_at: "2026-01-01T00:00:00Z",
  node_count: 4,
  edge_count: 1,
  truncated: false,
  nodes: [
    { id: "r1", kind: "requirement", label: "Single sign-on", source_ref: "r-uuid-1" },
    { id: "r2", kind: "requirement", label: "Audit log", source_ref: "r-uuid-2" },
    { id: "d1", kind: "decision", label: "Microsoft Entra ID", source_ref: "security_architecture[0]" },
    { id: "k1", kind: "risk", label: "Vendor lock-in", source_ref: "risks[0]" },
  ],
  edges: [
    {
      id: "e1",
      source_id: "d1",
      target_id: "r1",
      kind: "justified_by",
      evidence: "security_architecture[0].supporting_requirement_ids",
    },
  ],
};

const unbuilt: GraphData = {
  built: false,
  built_at: null,
  node_count: 0,
  edge_count: 0,
  truncated: false,
  nodes: [],
  edges: [],
};

function json(body: unknown, status = 200) {
  return { ok: status < 400, status, json: async () => body } as Response;
}

describe("GraphPanel", () => {
  const fetchMock = vi.fn();
  beforeEach(() => vi.stubGlobal("fetch", fetchMock));
  afterEach(() => {
    fetchMock.mockReset();
    vi.unstubAllGlobals();
  });

  it("offers a Build button for a proposal without a graph, then shows the graph", async () => {
    let isBuilt = false;
    fetchMock.mockImplementation(async (url: string, init?: RequestInit) => {
      if (url.endsWith("/graph/rebuild") && init?.method === "POST") {
        isBuilt = true;
        return json({ node_count: 4, edge_count: 1, skipped_requirement_refs: 2 });
      }
      if (url.endsWith("/graph/orphans")) return json({ orphans: [] });
      if (url.endsWith("/graph")) return json(isBuilt ? built : unbuilt);
      return json({}, 404);
    });
    render(<GraphPanel projectId={PID} proposalId={PROP} />);
    expect(await screen.findByText("Graph not built")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Build graph" }));
    expect(await screen.findByRole("region", { name: "Nodes: decision" })).toBeInTheDocument();
    expect(screen.getByText(/2 reference\(s\)/)).toBeInTheDocument();
  });

  it("lists nodes by kind, shows each edge's source field and the orphans", async () => {
    fetchMock.mockImplementation(async (url: string) => {
      if (url.endsWith("/graph/orphans")) {
        return json({ orphans: [{ id: "r2", kind: "requirement", label: "Audit log", source_ref: "x" }] });
      }
      return json(built);
    });
    render(<GraphPanel projectId={PID} proposalId={PROP} />);
    const reqs = await screen.findByRole("region", { name: "Nodes: requirement" });
    expect(within(reqs).getByText("Single sign-on")).toBeInTheDocument();
    expect(screen.getByRole("region", { name: "Nodes: risk" })).toBeInTheDocument();
    expect(screen.queryByRole("region", { name: "Nodes: passage" })).toBeNull();
    const edges = screen.getByRole("list", { name: "Edges" });
    expect(within(edges).getByText(/security_architecture\[0\]\.supporting_requirement_ids/)).toBeInTheDocument();
    expect(within(screen.getByRole("list", { name: "Orphan requirements" })).getByText("Audit log")).toBeInTheDocument();
  });

  it("shows the impact of a selected node with depth and via edge", async () => {
    fetchMock.mockImplementation(async (url: string) => {
      if (url.includes("/graph/impact")) {
        expect(url).toContain("node_id=r1");
        return json({
          root: built.nodes[0],
          mode: "direct",
          hub_threshold: 5,
          hubs_not_expanded: [],
          max_depth: 1,
          truncated: true,
          items: [
            {
              node: built.nodes[2],
              depth: 1,
              via_kind: "justified_by",
              via_evidence: "security_architecture[0].supporting_requirement_ids",
              reached_from: "r1",
              via_hub: false,
            },
          ],
        });
      }
      if (url.endsWith("/graph/orphans")) return json({ orphans: [] });
      return json(built);
    });
    render(<GraphPanel projectId={PID} proposalId={PROP} />);
    const reqs = await screen.findByRole("region", { name: "Nodes: requirement" });
    fireEvent.click(within(reqs).getByRole("button", { name: "Single sign-on" }));
    const list = await screen.findByRole("list", { name: "Impacted nodes" });
    expect(within(list).getByText(/Microsoft Entra ID — depth 1, justified_by/)).toBeInTheDocument();
    expect(screen.getByText(/More dependents exist/)).toBeInTheDocument();
    expect(fetchMock.mock.calls.some(([u]) => String(u).includes("mode=direct"))).toBe(true);
  });

  it("switches to transitive mode and shows hubs that were not expanded", async () => {
    fetchMock.mockImplementation(async (url: string) => {
      if (url.includes("/graph/impact")) {
        const transitive = url.includes("mode=transitive");
        return json({
          root: built.nodes[0],
          mode: transitive ? "transitive" : "direct",
          hub_threshold: 5,
          hubs_not_expanded: transitive ? [{ node: built.nodes[2], degree: 7 }] : [],
          max_depth: transitive ? 3 : 1,
          truncated: false,
          items: [],
        });
      }
      if (url.endsWith("/graph/orphans")) return json({ orphans: [] });
      return json(built);
    });
    render(<GraphPanel projectId={PID} proposalId={PROP} />);
    const reqs = await screen.findByRole("region", { name: "Nodes: requirement" });
    fireEvent.click(within(reqs).getByRole("button", { name: "Single sign-on" }));
    await screen.findByRole("list", { name: "Impacted nodes" }).catch(() => null);
    const group = await screen.findByRole("group", { name: "Impact mode" });
    expect(within(group).getByRole("button", { name: "Direct" })).toHaveAttribute("aria-pressed", "true");
    expect(screen.queryByText(/not expanded: shared by/)).toBeNull();
    fireEvent.click(within(group).getByRole("button", { name: "Transitive" }));
    expect(await screen.findByText(/not expanded: shared by 7 items/)).toBeInTheDocument();
    expect(fetchMock.mock.calls.some(([u]) => String(u).includes("mode=transitive"))).toBe(true);
  });

  it("shows an error when the graph cannot be loaded", async () => {
    fetchMock.mockResolvedValue(json({ detail: "boom" }, 500));
    render(<GraphPanel projectId={PID} proposalId={PROP} />);
    await waitFor(() => expect(screen.getByRole("alert")).toBeInTheDocument());
  });
});
