import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import ChangesSection from "./ChangesSection";
import type { ChangeDetail } from "./api";

function detail(over: Partial<ChangeDetail> = {}): ChangeDetail {
  return {
    id: "c1",
    key: "CR-001",
    text: "Add a Redis cache",
    author: "ana",
    status: "analysed",
    impact_class: "architecture",
    class_override: null,
    effective_class: "architecture",
    drift_flagged: false,
    created_at: "2026-01-01T00:00:00Z",
    impact: {
      impact_class: "architecture",
      reason: "Architecture impact: the request adds a data store.",
      signals: [],
      components: [{ id: "web", name: "Web UI" }],
      decisions: [],
      requirements: [],
    },
    overridden_by: null,
    override_reason: null,
    decided_by: null,
    decision_reason: null,
    drift: [],
    proposals: [],
    log: {
      architecture: [],
      plan_deltas: [],
      runs: [],
      patches: [],
      ci_runs: [],
      pull_request_url: null,
      requirement_ids: [],
    },
    ...over,
  };
}

function json(body: unknown, status = 200) {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

afterEach(() => vi.unstubAllGlobals());

describe("ChangesSection", () => {
  it("submits a request, shows the impact and lets a person override the class", async () => {
    let created = false;
    const calls: { url: string; method: string; body?: string }[] = [];
    let current = detail({ status: "draft", impact: null, impact_class: null });
    vi.stubGlobal(
      "fetch",
      vi.fn(async (url: string, init?: RequestInit) => {
        const method = init?.method ?? "GET";
        calls.push({ url, method, body: init?.body as string | undefined });
        if (url.endsWith("/analyse")) {
          current = detail();
          return json(current);
        }
        if (url.endsWith("/class")) {
          current = detail({
            class_override: "code_only",
            effective_class: "code_only",
            overridden_by: "bo",
            override_reason: "tiny",
          });
          return json(current);
        }
        if (method === "POST") {
          created = true;
          return json(current, 201);
        }
        if (/change-requests\/c1$/.test(url)) return json(current);
        return json(created ? [current] : []);
      }),
    );
    render(<ChangesSection projectId="p" />);
    expect(await screen.findByText("No change requests yet.")).toBeTruthy();

    fireEvent.change(screen.getByLabelText("Describe the change"), {
      target: { value: "Add a Redis cache" },
    });
    fireEvent.change(screen.getAllByLabelText("Your name")[0], {
      target: { value: "ana" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Submit change request" }));

    expect(await screen.findByText(/adds a data store/)).toBeTruthy();
    expect(screen.getAllByText("Architecture").length).toBeGreaterThan(0);
    expect(screen.getByText(/Web UI/)).toBeTruthy();

    const names = screen.getAllByLabelText("Your name");
    fireEvent.change(names[names.length - 1], { target: { value: "bo" } });
    fireEvent.change(screen.getByLabelText("Reason"), {
      target: { value: "tiny" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Treat as code only" }));
    await waitFor(() =>
      expect(screen.getByText(/overridden by bo: tiny/)).toBeTruthy(),
    );
    const put = calls.find((c) => c.method === "PUT");
    expect(JSON.parse(put?.body ?? "{}")).toEqual({
      impact_class: "code_only",
      by: "bo",
      reason: "tiny",
    });
  });

  it("shows the diff, the drift banner and the change log links", async () => {
    const current = detail({
      status: "in_progress",
      drift_flagged: true,
      drift: [
        {
          kind: "new_package",
          name: "payments",
          detail: "New top-level package 'payments' is not in the architecture.",
        },
      ],
      proposals: [
        {
          id: "a2",
          version: 2,
          status: "draft",
          revised_from_id: "a1",
          diff: {
            components: {
              added: [{ key: "cache", name: "Cache" }],
              removed: [],
              changed: [],
            },
            decisions: { added: [], removed: [], changed: [] },
            removals: [],
          },
        },
      ],
      log: {
        architecture: [
          { id: "a2", version: 2, status: "draft", parent_version: 1 },
        ],
        plan_deltas: [],
        runs: [{ id: "r1", task_key: "T-001", status: "finished" }],
        patches: [{ id: "pt1", task_key: "T-001", status: "accepted" }],
        ci_runs: [],
        pull_request_url: "https://example.test/pr/1",
        requirement_ids: ["q1"],
      },
    });
    vi.stubGlobal(
      "fetch",
      vi.fn(async (url: string) =>
        /change-requests\/c1$/.test(url) ? json(current) : json([current]),
      ),
    );
    render(<ChangesSection projectId="p" />);
    fireEvent.click(await screen.findByRole("button", { name: /CR-001/ }));
    expect(await screen.findByText(/Components added: Cache/)).toBeTruthy();
    expect(screen.getByText(/New top-level package 'payments'/)).toBeTruthy();
    expect(
      screen.getByRole("button", { name: "Propose architecture update" }),
    ).toBeTruthy();
    expect(screen.getByText(/from v1/)).toBeTruthy();
    expect(
      screen.getByRole("link", { name: "Pull request" }).getAttribute("href"),
    ).toBe("https://example.test/pr/1");
    expect(screen.getByText(/1 proposed requirement/)).toBeTruthy();
  });
});
