import {
  fireEvent,
  render,
  screen,
  waitFor,
  within,
} from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import ProjectsPage from "./ProjectsPage";
import type { Project } from "./api";

const base: Project = {
  id: "11111111-1111-1111-1111-111111111111",
  name: "Alpha",
  description: "First",
  status: "draft",
  created_at: "2026-01-01T00:00:00Z",
  updated_at: "2026-01-01T00:00:00Z",
};

function json(body: unknown, status = 200) {
  return { ok: status < 400, status, json: async () => body } as Response;
}

describe("ProjectsPage", () => {
  const fetchMock = vi.fn();

  beforeEach(() => vi.stubGlobal("fetch", fetchMock));
  afterEach(() => {
    fetchMock.mockReset();
    vi.unstubAllGlobals();
  });

  it("shows loading then the project list", async () => {
    fetchMock.mockResolvedValueOnce(json([base]));
    render(<ProjectsPage />);
    expect(screen.getByText("Loading projects…")).toBeInTheDocument();
    expect(
      await screen.findByRole("button", { name: "Alpha" }),
    ).toBeInTheDocument();
  });

  it("Open sets the active project and goes to the last section", async () => {
    localStorage.clear();
    fetchMock.mockResolvedValueOnce(json([base]));
    render(<ProjectsPage />);
    fireEvent.click(await screen.findByRole("button", { name: "Open Alpha" }));
    expect(localStorage.getItem("archpilot.activeProject")).toBe(base.id);
    expect(window.location.hash).toBe(`#/architecture/${base.id}`);

    localStorage.setItem(`archpilot.lastSection.${base.id}`, "documents");
    fireEvent.click(screen.getByRole("button", { name: "Open Alpha" }));
    expect(window.location.hash).toBe(`#/documents/${base.id}`);
  });

  it("shows an error and retries", async () => {
    fetchMock.mockRejectedValueOnce(new Error("Network down"));
    fetchMock.mockResolvedValueOnce(json([]));
    render(<ProjectsPage />);
    expect(await screen.findByText("Network down")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Retry" }));
    expect(await screen.findByText("No projects yet.")).toBeInTheDocument();
  });

  it("creates a project", async () => {
    fetchMock.mockResolvedValueOnce(json([]));
    render(<ProjectsPage />);
    await screen.findByText("No projects yet.");

    fetchMock.mockResolvedValueOnce(json(base, 201));
    fireEvent.click(screen.getByRole("button", { name: "New project" }));
    const dialog = screen.getByRole("dialog", { name: "New project" });
    fireEvent.change(within(dialog).getByLabelText("Name"), {
      target: { value: "  Alpha " },
    });
    fireEvent.click(
      within(dialog).getByRole("button", { name: "Create project" }),
    );

    expect(
      await screen.findByRole("button", { name: "Alpha" }),
    ).toBeInTheDocument();
    expect(screen.queryByRole("dialog")).toBeNull();
    const [url, init] = fetchMock.mock.calls[1];
    expect(url).toMatch(/\/api\/projects$/);
    expect(JSON.parse(init.body)).toEqual({
      name: "Alpha",
      description: null,
      status: "draft",
    });
  });

  it("validates the name before submitting", async () => {
    fetchMock.mockResolvedValueOnce(json([]));
    render(<ProjectsPage />);
    await screen.findByText("No projects yet.");
    fireEvent.click(screen.getByRole("button", { name: "New project" }));
    fireEvent.click(screen.getByRole("button", { name: "Create project" }));
    expect(await screen.findByText("Name is required")).toBeInTheDocument();
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });

  it("shows server errors from create", async () => {
    fetchMock.mockResolvedValueOnce(json([]));
    render(<ProjectsPage />);
    await screen.findByText("No projects yet.");
    fireEvent.click(screen.getByRole("button", { name: "New project" }));
    fetchMock.mockResolvedValueOnce(
      json({ detail: [{ msg: "String too long" }] }, 422),
    );
    fireEvent.change(screen.getByLabelText("Name"), { target: { value: "x" } });
    fireEvent.click(screen.getByRole("button", { name: "Create project" }));
    expect(await screen.findByText("String too long")).toBeInTheDocument();
  });

  it("views and edits a project", async () => {
    fetchMock.mockResolvedValueOnce(json([base]));
    render(<ProjectsPage />);
    fireEvent.click(await screen.findByRole("button", { name: "Alpha" }));
    const details = screen.getByRole("region", { name: "Project details" });
    expect(within(details).getByText("draft")).toBeInTheDocument();

    fireEvent.click(within(details).getByRole("button", { name: "Edit" }));
    const edit = screen.getByRole("dialog", { name: "Edit project" });
    fetchMock.mockResolvedValueOnce(
      json({ ...base, name: "Beta", status: "active" }),
    );
    fireEvent.change(within(edit).getByLabelText("Name"), {
      target: { value: "Beta" },
    });
    fireEvent.change(within(edit).getByLabelText("Status"), {
      target: { value: "active" },
    });
    fireEvent.click(within(edit).getByRole("button", { name: "Save changes" }));

    expect(await screen.findByText("active", { selector: "section[aria-label='Project details'] .badge" })).toBeInTheDocument();
    expect(screen.queryByRole("dialog")).toBeNull();
    const [url, init] = fetchMock.mock.calls.find(
      (c) => c[1]?.method === "PATCH",
    )!;
    expect(url).toMatch(new RegExp(`/api/projects/${base.id}$`));
    expect(init.method).toBe("PATCH");
  });

  it("requires confirmation before deleting", async () => {
    fetchMock.mockResolvedValueOnce(json([base]));
    render(<ProjectsPage />);
    fireEvent.click(await screen.findByRole("button", { name: "Alpha" }));

    fireEvent.click(screen.getByRole("button", { name: "Delete" }));
    const deletes = () =>
      fetchMock.mock.calls.filter((c) => c[1]?.method === "DELETE");
    expect(deletes()).toHaveLength(0);
    fireEvent.click(screen.getByRole("button", { name: "Keep project" }));
    expect(screen.getByRole("button", { name: "Delete" })).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "Delete" }));
    fetchMock.mockResolvedValueOnce({ ok: true, status: 204 } as Response);
    fireEvent.click(screen.getByRole("button", { name: "Confirm delete" }));

    await waitFor(() =>
      expect(screen.getByText("No projects yet.")).toBeInTheDocument(),
    );
    expect(deletes()).toHaveLength(1);
  });

  it("shows an error when delete fails", async () => {
    fetchMock.mockResolvedValueOnce(json([base]));
    render(<ProjectsPage />);
    fireEvent.click(await screen.findByRole("button", { name: "Alpha" }));
    fireEvent.click(screen.getByRole("button", { name: "Delete" }));
    fetchMock.mockResolvedValueOnce(json({ detail: "Project not found" }, 404));
    fireEvent.click(screen.getByRole("button", { name: "Confirm delete" }));
    expect(await screen.findByText("Project not found")).toBeInTheDocument();
  });
});

