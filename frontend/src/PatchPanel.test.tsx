import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import PatchPanel from "./PatchPanel";
import type { CodePatchDetail, PlanTask, RemovalsNotConfirmedDetail } from "./api";
import patchFixture from "./fixtures/patch_with_removals.json";
import conflictFixtureJson from "./fixtures/removals_not_confirmed.json";

const conflictFixture: { detail: RemovalsNotConfirmedDetail } = {
  detail: conflictFixtureJson.detail as RemovalsNotConfirmedDetail,
};

const PID = "p";

function task(over: Partial<PlanTask> = {}): PlanTask {
  return {
    id: "t1",
    key: "T-001",
    title: "Impl",
    kind: "component",
    description: "",
    source_ref: "",
    component_ref: null,
    requirement_ids: [],
    acceptance_criteria: [],
    depends_on: [],
    dependencies: [],
    flags: [],
    blocking_questions: [],
    notes: [],
    related: [],
    order_index: 1,
    status: "todo",
    ...over,
  } as PlanTask;
}

function patch(over: Partial<CodePatchDetail> = {}): CodePatchDetail {
  return {
    id: "pa1",
    task_key: "T-001",
    version: 1,
    status: "proposed",
    provider: "fake",
    model: "fake",
    prompt_version: "v1",
    created_at: "2026-01-01T00:00:00Z",
    reviewed_by: null,
    review_reason: null,
    file_count: 1,
    plan_id: "pl",
    task_id: "t1",
    notes: ["a note"],
    assumptions: [],
    test_suggestions: [],
    warnings: ["dependency T-000 has no accepted patch"],
    blocking_acknowledged: false,
    validation: "passed",
    stale: [],
    files: [
      {
        path: "src/a.py",
        change: "create",
        content: "x = 1\n",
        previous_content_hash: null,
        diff: "+x = 1",
        diff_truncated: false,
      },
    ],
    ...over,
  };
}

function json(body: unknown, status = 200) {
  return { ok: status < 400, status, json: async () => body } as Response;
}

afterEach(() => vi.unstubAllGlobals());

function setup(t: PlanTask, handler: (url: string, init?: RequestInit) => Response) {
  const fetchMock = vi.fn(async (url: string, init?: RequestInit) => handler(url, init));
  vi.stubGlobal("fetch", fetchMock);
  const onChanged = vi.fn();
  render(<PatchPanel projectId={PID} planId="pl" task={t} approved onChanged={onChanged} />);
  return { fetchMock, onChanged };
}

describe("PatchPanel", () => {
  it("renders nothing for an unapproved plan", () => {
    vi.stubGlobal("fetch", vi.fn());
    const { container } = render(
      <PatchPanel projectId={PID} planId="pl" task={task()} approved={false} onChanged={() => {}} />,
    );
    expect(container).toBeEmptyDOMElement();
  });

  it("lists the file count and the feedback origin of each version, and the coverage", async () => {
    setup(task(), () =>
      json([patch({ id: "pa2", version: 2, file_count: 3, based_on_version: 1 })]),
    );
    expect(await screen.findByText(/3 file\(s\)/)).toBeInTheDocument();
    expect(screen.getByText(/Based on feedback from version 1/)).toBeInTheDocument();
  });

  it("shows covered and missing items of the detail", async () => {
    const coverage = {
      covered: 1,
      missing: 1,
      unchecked: 0,
      items: [
        { kind: "criterion", ref: "AC1", text: "Admin approves", status: "covered", covered: ["admin"], missing: [] },
        { kind: "criterion", ref: "AC2", text: "Customer logs in", status: "missing", covered: [], missing: ["customer"] },
      ],
    } as const;
    setup(task(), (_url, init) =>
      init?.method === "POST"
        ? json(patch({ coverage: coverage as never, based_on_version: 1 }), 201)
        : json([]),
    );
    fireEvent.click(await screen.findByRole("button", { name: "Generate patch" }));
    expect(await screen.findByText(/missing: customer/)).toBeInTheDocument();
    expect(screen.getByText("1 covered · 1 missing · 0 unchecked")).toBeInTheDocument();
    expect(screen.getAllByText(/Based on feedback from version 1/).length).toBeGreaterThan(0);
  });

  it("shows unchecked as its own state with a reason, plus removals and scope", async () => {
    const coverage = {
      covered: 3,
      missing: 0,
      unchecked: 9,
      items: [
        { kind: "criterion", ref: "AC4", text: "It works well", status: "unchecked", covered: [], missing: [], reason: "no key terms extracted", prompt: "check manually" },
      ],
      removed: { since: 1, items: ["class Invoice", "field Invoice.total"] },
      scope: ["Requirement FR-002 (admin) is related to T-002, not in this task's scope"],
    };
    setup(task(), (_url, init) =>
      init?.method === "POST" ? json(patch({ coverage: coverage as never }), 201) : json([]),
    );
    fireEvent.click(await screen.findByRole("button", { name: "Generate patch" }));
    expect(await screen.findByText("3 covered · 0 missing · 9 unchecked")).toBeInTheDocument();
    expect(screen.getByText(/no key terms extracted: check manually/)).toBeInTheDocument();
    expect(screen.getByText("Removed since v1")).toBeInTheDocument();
    expect(screen.getByText("field Invoice.total")).toBeInTheDocument();
    expect(screen.getByText(/not in this task's scope/)).toBeInTheDocument();
  });

  it("generates a patch and shows diff, warnings and notes", async () => {
    const { onChanged } = setup(task(), (_url, init) => {
      if (init?.method === "POST") return json(patch(), 201);
      return json([]);
    });
    fireEvent.click(await screen.findByRole("button", { name: "Generate patch" }));
    expect(await screen.findByText("+x = 1")).toBeInTheDocument();
    expect(screen.getByText(/has no accepted patch/)).toBeInTheDocument();
    expect(screen.getByText("a note")).toBeInTheDocument();
    expect(onChanged).toHaveBeenCalled();
  });

  it("shows a gate error with its message", async () => {
    setup(task(), (_url, init) =>
      init?.method === "POST"
        ? json({ detail: { code: "blocking_questions", message: "Blocking questions open" } }, 409)
        : json([]),
    );
    fireEvent.click(await screen.findByRole("button", { name: "Generate patch" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("Blocking questions open");
  });

  it("shows validation failure codes and paths", async () => {
    setup(task(), (_url, init) =>
      init?.method === "POST"
        ? json(
            { detail: { message: "Patch rejected", failures: [{ code: "path_traversal", path: "../x.py" }] } },
            422,
          )
        : json([]),
    );
    fireEvent.click(await screen.findByRole("button", { name: "Generate patch" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("path_traversal (../x.py)");
  });

  it("sends the blocking-question acknowledgement", async () => {
    const { fetchMock } = setup(task({ blocking_questions: ["Which IdP?"] }), (_url, init) =>
      init?.method === "POST" ? json(patch(), 201) : json([]),
    );
    fireEvent.click(await screen.findByLabelText(/Acknowledge blocking questions/));
    fireEvent.click(screen.getByRole("button", { name: "Generate patch" }));
    await screen.findByText("+x = 1");
    const post = fetchMock.mock.calls.find((c) => c[1]?.method === "POST");
    expect(JSON.parse(String(post?.[1]?.body))).toEqual({ acknowledge_blocking_questions: true });
  });

  it("records a technology confirmation for AI-flagged tasks", async () => {
    const { fetchMock } = setup(
      task({ flags: ["AI recommendation: confirm technology before implementing"] }),
      (_url, init) =>
        init?.method === "POST"
          ? json({ task_key: "T-001", name: "FastAPI", reason: "team", created_at: "" }, 201)
          : json([]),
    );
    fireEvent.change(await screen.findByLabelText("Technology name"), { target: { value: "FastAPI" } });
    fireEvent.change(screen.getByLabelText("Reason"), { target: { value: "team" } });
    fireEvent.click(screen.getByRole("button", { name: "Record confirmation" }));
    await screen.findByText("Technology confirmed.");
    const post = fetchMock.mock.calls.find((c) => String(c[0]).endsWith("technology-confirmation"));
    expect(post?.[1]?.method).toBe("POST");
  });

  it("accepts a patch with reviewer and reason, then hides review controls", async () => {
    const summary = { ...patch(), files: undefined };
    setup(task(), (url) => {
      if (url.endsWith("/accept")) {
        return json(patch({ status: "accepted", reviewed_by: "Ann", review_reason: "ok" }));
      }
      if (url.endsWith("/patches/pa1")) return json(patch());
      if (url.endsWith("/patches")) return json([summary]);
      return json({}, 404);
    });
    fireEvent.click(await screen.findByRole("button", { name: "v1" }));
    const accept = await screen.findByRole("button", { name: "Accept patch" });
    expect(accept).toBeDisabled();
    fireEvent.change(screen.getByLabelText("Reviewer"), { target: { value: "Ann" } });
    fireEvent.change(screen.getByLabelText("Review reason"), { target: { value: "ok" } });
    fireEvent.click(accept);
    await waitFor(() => expect(screen.queryByRole("button", { name: "Accept patch" })).toBeNull());
    expect(screen.getByText(/Reviewed by Ann/)).toBeInTheDocument();
  });

  it("flags a stale patch", async () => {
    const summary = { ...patch(), files: undefined };
    setup(task(), (url) => {
      if (url.endsWith("/patches/pa1")) {
        return json(patch({ validation: "stale", stale: [{ code: "stale_patch", path: "src/a.py" }] }));
      }
      return json([summary]);
    });
    fireEvent.click(await screen.findByRole("button", { name: "v1" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("Stale patch");
  });

  describe("removals from accepted file", () => {
    const removed = [
      { kind: "class", name: "Invoice", file: "src/a.py", listed: true, reason: "removed" },
      { kind: "function", name: "total", file: "src/a.py", listed: false, reason: "removed" },
    ];

    it("shows no removal section for patches without removals", async () => {
      setup(task(), (url) =>
        url.endsWith("/patches/pa1") ? json(patch()) : json([{ ...patch(), files: undefined }]),
      );
      fireEvent.click(await screen.findByRole("button", { name: "v1" }));
      await screen.findByRole("button", { name: "Accept patch" });
      expect(screen.queryByText(/Removed from accepted file/)).toBeNull();
    });

    it("shows listed vs unintended badges", async () => {
      setup(task(), (url) => {
        if (url.endsWith("/patches/pa1"))
          return json(patch({ removed_from_accepted: removed, needs_justification: true }));
        return json([{ ...patch(), files: undefined }]);
      });
      fireEvent.click(await screen.findByRole("button", { name: "v1" }));
      expect(await screen.findByText("listed")).toHaveClass("badge");
      expect(screen.getByText("unintended")).toHaveClass("badge-warn");
      expect(screen.queryByText("listed")).not.toHaveClass("badge-warn");
    });

    it("does not show the section when the patch does not need justification", async () => {
      setup(task(), (url) =>
        url.endsWith("/patches/pa1")
          ? json(patch({ removed_from_accepted: removed, needs_justification: false }))
          : json([{ ...patch(), files: undefined }]),
      );
      fireEvent.click(await screen.findByRole("button", { name: "v1" }));
      await screen.findByRole("button", { name: "Accept patch" });
      expect(screen.queryByText(/Removed from accepted file/)).toBeNull();
    });

    it("matches the backend contract fixtures", async () => {
      const detail = patchFixture as unknown as CodePatchDetail;
      expect(Object.keys(detail.removed_from_accepted![0]).sort()).toEqual(
        ["file", "kind", "listed", "name", "reason"],
      );
      expect(detail.needs_justification).toBe(true);
      expect(Object.keys(conflictFixture.detail).sort()).toEqual(["code", "message", "missing"]);
      expect(Object.keys(conflictFixture.detail.missing[0]).sort()).toEqual(["file", "name"]);
      setup(task(), (url) =>
        url.endsWith(`/patches/${detail.id}`) ? json(detail) : json([{ ...detail, files: undefined }]),
      );
      fireEvent.click(await screen.findByRole("button", { name: "v1" }));
      expect(await screen.findByText("Removed from accepted file (2)")).toBeInTheDocument();
      expect(screen.getByText("listed")).toBeInTheDocument();
      expect(screen.getByText("unintended")).toBeInTheDocument();
    });

    it("gates accept on every row and sends confirmed_removals", async () => {
      const { fetchMock } = setup(task(), (url) => {
        if (url.endsWith("/accept")) return json(patch({ status: "accepted" }));
        if (url.endsWith("/patches/pa1")) return json(patch({ removed_from_accepted: removed, needs_justification: true }));
        return json([{ ...patch(), files: undefined }]);
      });
      fireEvent.click(await screen.findByRole("button", { name: "v1" }));
      expect(await screen.findByText("Removed from accepted file (2)")).toBeInTheDocument();
      expect(screen.getByText("listed")).toBeInTheDocument();
      expect(screen.getByText("unintended")).toBeInTheDocument();
      fireEvent.change(screen.getByLabelText("Reviewer"), { target: { value: "Ann" } });
      fireEvent.change(screen.getByLabelText("Review reason"), { target: { value: "ok" } });
      const accept = screen.getByRole("button", { name: "Accept patch" });
      expect(accept).toBeDisabled();
      for (const r of removed) {
        const label = `${r.kind} ${r.name} in ${r.file}`;
        fireEvent.click(screen.getByLabelText(`Confirm removal of ${label}`));
        expect(accept).toBeDisabled();
        fireEvent.change(screen.getByLabelText(`Removal reason for ${label}`), {
          target: { value: `drop ${r.name}` },
        });
      }
      expect(accept).toBeEnabled();
      fireEvent.click(accept);
      await waitFor(() => expect(screen.queryByRole("button", { name: "Accept patch" })).toBeNull());
      const post = fetchMock.mock.calls.find((c) => String(c[0]).endsWith("/accept"));
      expect(JSON.parse(String(post?.[1]?.body)).confirmed_removals).toEqual([
        { file: "src/a.py", name: "Invoice", reason: "drop Invoice" },
        { file: "src/a.py", name: "total", reason: "drop total" },
      ]);
    });

    it("lists unconfirmed items on 409 removals_not_confirmed", async () => {
      setup(task(), (url) => {
        if (url.endsWith("/accept")) return json(conflictFixture, 409);
        if (url.endsWith("/patches/pa1")) return json(patch({ removed_from_accepted: removed, needs_justification: true }));
        return json([{ ...patch(), files: undefined }]);
      });
      fireEvent.click(await screen.findByRole("button", { name: "v1" }));
      await screen.findByText("Removed from accepted file (2)");
      fireEvent.change(screen.getByLabelText("Reviewer"), { target: { value: "Ann" } });
      fireEvent.change(screen.getByLabelText("Review reason"), { target: { value: "ok" } });
      for (const r of removed) {
        const label = `${r.kind} ${r.name} in ${r.file}`;
        fireEvent.click(screen.getByLabelText(`Confirm removal of ${label}`));
        fireEvent.change(screen.getByLabelText(`Removal reason for ${label}`), {
          target: { value: "x" },
        });
      }
      fireEvent.click(screen.getByRole("button", { name: "Accept patch" }));
      expect(await screen.findByText(/Unconfirmed removals: total \(src\/a\.py\)/)).toBeInTheDocument();
    });
  });
});
